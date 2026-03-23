"""
Structured logging with daily file rotation, per-request tracing, and colors.

Every log line carries ``session_id`` (long-lived session) and ``trace_id``
(one per user turn) so you can correlate all work triggered by a single
user message.

Usage in main.py::

    from app.middleware.logging_config import setup_logging
    setup_logging()                          # call once at startup

Usage in request handlers::

    from app.middleware.logging_config import set_trace_context
    set_trace_context(session_id, trace_id)  # call at top of each turn

Colors:
    Console  — always colored (auto-detected via isatty, force with LOG_COLORS=true)
    Log files — colored by default so ``tail -f`` / ``less -R`` show colors.
                Disable with LOG_FILE_COLORS=false if you parse logs with tools
                that don't understand ANSI codes.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# ── Context variables (set per-request, read by every log line) ─────────────

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")


def set_trace_context(session_id: str, trace_id: str | None = None) -> str:
    """Set the per-request trace context.  Returns the trace_id used."""
    tid = trace_id or uuid.uuid4().hex[:8]
    session_id_var.set(session_id)
    trace_id_var.set(tid)
    return tid


def clear_trace_context() -> None:
    trace_id_var.set("-")
    session_id_var.set("-")


# ── Filter that injects context vars into every record ──────────────────────

class _TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get("-")      # type: ignore[attr-defined]
        record.session_id = session_id_var.get("-")   # type: ignore[attr-defined]
        return True


# ── ANSI Color Codes ────────────────────────────────────────────────────────

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"

# Level colors
_COLORS = {
    "DEBUG":    "\033[36m",       # cyan
    "INFO":     "\033[32m",       # green
    "WARNING":  "\033[33m",       # yellow
    "ERROR":    "\033[31m",       # red
    "CRITICAL": "\033[1;31m",     # bold red
}

# Field highlight colors
_SID_COLOR = "\033[35m"           # magenta for session id
_TID_COLOR = "\033[34m"           # blue for trace id
_NAME_COLOR = "\033[90m"          # gray for logger name
_TIME_COLOR = "\033[37m"          # white/light gray for timestamp


class ColorFormatter(logging.Formatter):
    """Formatter that injects ANSI color codes per log level + field highlights.

    Builds a separate format string per level so we never mutate the LogRecord
    (which would cause double-escape issues when multiple handlers share it).
    """

    def __init__(self, fmt: str, use_colors: bool = True):
        super().__init__(fmt)
        self._use_colors = use_colors
        self._plain_fmt = fmt
        # Pre-build one colored format string per level
        self._level_fmts: dict[str, str] = {}
        if use_colors:
            for level_name, color in _COLORS.items():
                colored = fmt
                # %(levelname)-5s  →  colored level
                colored = colored.replace(
                    "%(levelname)-5s",
                    f"{color}%(levelname)-5s{_RESET}",
                )
                # %(name)-Ns  →  gray name
                for width in ("25", "30"):
                    colored = colored.replace(
                        f"%(name)-{width}s",
                        f"{_NAME_COLOR}%(name)-{width}s{_RESET}",
                    )
                # sid= and tid= highlights
                colored = colored.replace(
                    "sid=%(session_id)s",
                    f"sid={_SID_COLOR}%(session_id)s{_RESET}",
                )
                colored = colored.replace(
                    "tid=%(trace_id)s",
                    f"tid={_TID_COLOR}%(trace_id)s{_RESET}",
                )
                # %(message)s  →  level-colored message
                colored = colored.replace(
                    "%(message)s",
                    f"{color}%(message)s{_RESET}",
                )
                self._level_fmts[level_name] = colored

    def format(self, record: logging.LogRecord) -> str:
        if self._use_colors and record.levelname in self._level_fmts:
            # Swap format string for this level, format, swap back
            self._style._fmt = self._level_fmts[record.levelname]
            result = super().format(record)
            self._style._fmt = self._plain_fmt
            return result
        return super().format(record)


# ── Setup ───────────────────────────────────────────────────────────────────

_LOG_DIR = os.getenv("LOG_DIR", "logs")

# Formats
_FILE_FMT = (
    "%(asctime)s | %(levelname)-5s | %(name)-30s "
    "| sid=%(session_id)s | tid=%(trace_id)s | %(message)s"
)
_CONSOLE_FMT = (
    "%(asctime)s %(levelname)-5s %(name)-25s "
    "| sid=%(session_id)s | tid=%(trace_id)s | %(message)s"
)

# Noisy third-party loggers we want to silence
_QUIET_LOGGERS = [
    "httpx", "httpcore", "urllib3", "asyncio",
    "mcp", "openai", "anthropic", "watchfiles",
    "uvicorn.access",
]


def setup_logging(
    log_dir: str = _LOG_DIR,
    log_level: str | None = None,
) -> None:
    """Initialise logging — call once at startup before any other import."""
    from app.config import CONFIG
    level_name = (log_level or CONFIG.observability.log_level).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Color settings
    file_colors = os.getenv("LOG_FILE_COLORS", "true").lower() == "true"
    console_colors = os.getenv("LOG_COLORS", "true").lower() == "true"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)          # capture everything; handlers filter
    root.handlers.clear()

    trace_filter = _TraceFilter()

    # ── 1. Main application log (daily rotation, 30-day retention) ───────
    app_handler = TimedRotatingFileHandler(
        filename=str(log_path / "agent.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    app_handler.suffix = "%Y-%m-%d"
    app_handler.setLevel(level)
    app_handler.setFormatter(ColorFormatter(_FILE_FMT, use_colors=file_colors))
    app_handler.addFilter(trace_filter)
    root.addHandler(app_handler)

    # ── 2. Audit log (separate file, 90-day retention) ───────────────────
    audit_handler = TimedRotatingFileHandler(
        filename=str(log_path / "audit.log"),
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8",
    )
    audit_handler.suffix = "%Y-%m-%d"
    audit_handler.setLevel(logging.INFO)
    audit_handler.setFormatter(ColorFormatter(_FILE_FMT, use_colors=file_colors))
    audit_handler.addFilter(trace_filter)
    audit_logger = logging.getLogger("sap_agent.audit")
    audit_logger.addHandler(audit_handler)
    audit_logger.propagate = True          # also appears in agent.log

    # ── 3. Error log (errors + critical only, 60-day retention) ──────────
    error_handler = TimedRotatingFileHandler(
        filename=str(log_path / "error.log"),
        when="midnight",
        interval=1,
        backupCount=60,
        encoding="utf-8",
    )
    error_handler.suffix = "%Y-%m-%d"
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(ColorFormatter(_FILE_FMT, use_colors=file_colors))
    error_handler.addFilter(trace_filter)
    root.addHandler(error_handler)

    # ── 4. Console ───────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(ColorFormatter(_CONSOLE_FMT, use_colors=console_colors))
    console.addFilter(trace_filter)
    root.addHandler(console)

    # ── Quiet noisy third-party loggers ──────────────────────────────────
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # ── Per-module log level overrides ───────────────────────────────────
    # LOG_LEVEL_OVERRIDES=sap_agent.mcp:DEBUG,sap_agent.agent_service:DEBUG
    overrides = os.getenv("LOG_LEVEL_OVERRIDES", "")
    if overrides:
        for pair in overrides.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            logger_name, lvl = pair.split(":", 1)
            override_level = getattr(logging, lvl.strip().upper(), None)
            if override_level is not None:
                logging.getLogger(logger_name.strip()).setLevel(override_level)

    logging.getLogger("sap_agent").info(
        "Logging initialised | dir=%s level=%s colors=console:%s,file:%s",
        log_path.resolve(), level_name, console_colors, file_colors,
    )

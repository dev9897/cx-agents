


"""
main.py
=======
THE SINGLE ENTRYPOINT for the SAP Commerce Shopping Agent.

Usage:
    python main.py                    # starts FastAPI server (default)
    python main.py --mode cli         # starts interactive CLI
    python main.py --mode server      # starts FastAPI server explicitly
    python main.py --mode check       # validates config & connectivity only

Changes from previous version
------------------------------
1.  [FIX]  run_server(): host env-var key corrected from "localhost" to "HOST".
           The old code always resolved to None, so host defaulted to "0.0.0.0"
           accidentally — it now honours a HOST env var correctly.
2.  [FIX]  run_cli(): interrupt check now looks for GraphInterrupt exception
           rather than a state key "__interrupt__" which LangGraph never sets.
3.  [IMPROVE] validate_env(): prints the actual value (masked) so it is easier
              to spot copy-paste mistakes in .env.
4.  [IMPROVE] run_server(): log includes all relevant config in one place.
5.  [IMPROVE] Removed dead commented-out Anthropic check — confusing to leave
              in a file that now uses Ollama.
6.  [CLEAN]  check_connectivity(): extracted _check_ssl_info() helper so the
             SSL diagnostic block isn't inlined inside error handling.
"""

from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Load .env FIRST — several local modules read env vars at import time.
# ─────────────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Validate required environment variables before booting anything heavy.
# ─────────────────────────────────────────────────────────────────────────────
REQUIRED_ENV_VARS = [
    "SAP_BASE_URL",
    "SAP_SITE_ID",
    "SAP_CLIENT_ID",
    "SAP_CLIENT_SECRET",
]

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sap_agent.main")


def _mask(value: str, show: int = 4) -> str:
    """Return the first `show` chars of a secret followed by asterisks."""
    if not value:
        return "(empty)"
    return value[:show] + "*" * max(0, len(value) - show)


def validate_env() -> bool:
    """
    Check all required env vars are present and non-empty.
    Prints a masked preview of each value so copy-paste mistakes are obvious.
    Returns True only if every var is set.
    """
    missing = []
    print("\n  Environment variable check:")
    for var in REQUIRED_ENV_VARS:
        val = os.getenv(var, "")
        if val:
            print(f"    ✅  {var} = {_mask(val)}")
        else:
            print(f"    ❌  {var} — NOT SET")
            missing.append(var)

    if missing:
        print(f"\n  ❌  {len(missing)} variable(s) missing.")
        print("      Copy .env.example → .env and fill in the values.")
        return False

    print("  ✅  All required environment variables are present.\n")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Deferred import of heavy local modules (after env is loaded).
# ─────────────────────────────────────────────────────────────────────────────

def _import_modules():
    """Import heavy dependencies only after env validation passes."""
    global agent_config, production_agent, api_server
    import agent_config     as _ac; agent_config     = _ac   # noqa: E702
    import production_agent as _pa; production_agent = _pa   # noqa: E702
    import api_server       as _as; api_server       = _as   # noqa: E702


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Connectivity check
# ─────────────────────────────────────────────────────────────────────────────

def _log_ssl_info() -> None:
    """Log the active OpenSSL version and CA bundle paths."""
    try:
        paths = ssl.get_default_verify_paths()
        logger.debug("   SSL openssl  = %s", ssl.OPENSSL_VERSION)
        logger.debug("   SSL cafile   = %s", paths.cafile)
        logger.debug("   SSL capath   = %s", paths.capath)
    except Exception as exc:
        logger.debug("   Could not read SSL paths: %s", exc)


def check_connectivity() -> bool:
    """
    Perform a lightweight GET against the SAP OCC catalog endpoint.
    Returns True if the server responds with HTTP < 500.
    """
    import httpx

    base = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
    site = os.getenv("SAP_SITE_ID", "")
    url  = f"{base}/{site}/catalogs"

    logger.info("🔍 SAP connectivity check → %s", url)
    _log_ssl_info()

    try:
        r = httpx.get(url, timeout=10)
        if r.status_code < 500:
            logger.info("✅ SAP OCC API reachable (status=%d)", r.status_code)
            print(f"  ✅  SAP OCC API reachable (HTTP {r.status_code})")
            return True
        logger.warning("⚠️  SAP OCC API returned %d", r.status_code)
        print(f"  ⚠️   SAP OCC API returned {r.status_code} — check credentials")
        return False

    except httpx.ConnectError as exc:
        cause = str(exc.__cause__ or exc)
        if "CERTIFICATE_VERIFY_FAILED" in cause or "SSL" in cause.upper():
            logger.error(
                "🔒 SSL certificate verification failed\n"
                "   URL   : %s\n"
                "   Cause : %s\n"
                "   Fix   : Add the SAP server CA cert to your trust store, or\n"
                "           set SAP_SSL_VERIFY=false (dev only).",
                url, cause,
            )
        else:
            logger.error("❌ Connection error | url=%s | %s", url, cause)
        print(f"  ❌  Cannot reach SAP OCC API: {exc}")
        return False

    except httpx.TimeoutException as exc:
        logger.error("⏱️  Timeout reaching SAP OCC API | url=%s | %s", url, exc)
        print(f"  ❌  Timeout reaching SAP OCC API: {exc}")
        return False

    except Exception as exc:
        logger.exception("❌ Unexpected error during connectivity check | url=%s", url)
        print(f"  ❌  Cannot reach SAP OCC API: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MODE A: CLI interactive session
# ─────────────────────────────────────────────────────────────────────────────

def run_cli():
    from production_agent import get_last_ai_message, new_session, run_turn

    # LangGraph raises GraphInterrupt (not sets a state key) when an interrupt
    # fires — import it so we can catch it properly (fix #2).
    try:
        from langgraph.errors import GraphInterrupt
    except ImportError:
        GraphInterrupt = None   # older LangGraph versions don't have this

    print("\n" + "=" * 60)
    print("  🛒  SAP Commerce Shopping Agent  —  CLI Mode")
    print("=" * 60)
    print("  Type your request. Type 'quit' to exit.\n")

    user_id = input("  Your user ID (press Enter for 'guest'): ").strip() or "guest"
    session_state, thread_id = new_session(user_id)
    print(f"\n  Session started: {thread_id}\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        try:
            session_state = run_turn(user_input, thread_id, session_state)
        except Exception as exc:
            # Handle human-approval interrupt from LangGraph (fix #2)
            if GraphInterrupt and isinstance(exc, GraphInterrupt):
                interrupt_data = exc.args[0] if exc.args else {}
                message = (
                    interrupt_data.get("message", "Please confirm to continue.")
                    if isinstance(interrupt_data, dict) else str(interrupt_data)
                )
                print(f"\n  ⚠️  Confirmation required: {message}")
                answer   = input("   Approve? (yes/no): ").strip().lower()
                approved = answer in ("yes", "y")
                try:
                    session_state = run_turn(
                        "",
                        thread_id,
                        session_state,
                        approval_response={"approved": approved},
                    )
                except Exception as resume_exc:
                    print(f"\n  ❌  Error resuming: {resume_exc}\n")
                    continue
            else:
                print(f"\n  ❌  Error: {exc}\n")
                logger.exception("run_cli | unexpected error")
                continue

        reply = get_last_ai_message(session_state)
        if reply:
            print(f"\nAssistant: {reply}\n")

        turn = session_state.get("turn_count", 0)
        if turn > 0 and turn % 5 == 0:
            tin  = session_state.get("total_input_tokens",  0)
            tout = session_state.get("total_output_tokens", 0)
            print(f"  💡 [{turn} turns | tokens in={tin} out={tout}]\n")


# ─────────────────────────────────────────────────────────────────────────────
# MODE B: FastAPI server  (fix #1 — HOST env-var key was "localhost")
# ─────────────────────────────────────────────────────────────────────────────

def run_server():
    import uvicorn
    from agent_config import CONFIG

    # Fix #1: os.getenv("localhost", ...) always returned the default because
    # "localhost" is not a valid env-var name — corrected to "HOST".
    host   = os.getenv("HOST", "0.0.0.0")
    port   = int(os.getenv("PORT", "8004"))
    env    = os.getenv("ENVIRONMENT", "production")
    reload = (env == "development")

    print(f"\n🚀  Starting SAP Commerce Agent API")
    print(f"    Host        : {host}:{port}")
    print(f"    Environment : {env}")
    print(f"    LLM model   : {os.getenv('OLLAMA_MODEL', 'qwen2.5:1.5b')}")
    print(f"    Hot-reload  : {reload}")
    print(f"    Docs        : http://localhost:{port}/docs\n")

    uvicorn.run(
        "api_server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=CONFIG.observability.log_level.lower(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# MODE C: Config / connectivity check only
# ─────────────────────────────────────────────────────────────────────────────

def run_check():
    print("\n🔍  Running pre-flight checks...\n")
    env_ok = validate_env()
    if not env_ok:
        sys.exit(1)

    sap_ok = check_connectivity()

    print()
    if env_ok and sap_ok:
        print("  ✅  All checks passed. Agent is ready to run.")
    else:
        print("  ❌  Some checks failed. Fix the issues above before starting.")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SAP Commerce Cloud Shopping Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py               # start API server\n"
            "  python main.py --mode cli    # interactive terminal\n"
            "  python main.py --mode check  # pre-flight check only\n"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["server", "cli", "check"],
        default="server",
        help="server = FastAPI API (default) | cli = terminal | check = pre-flight only",
    )
    args = parser.parse_args()

    # Always validate env first regardless of mode
    if not validate_env():
        sys.exit(1)

    if args.mode == "check":
        run_check()
    elif args.mode == "cli":
        _import_modules()
        run_cli()
    else:  # "server" (default)
        _import_modules()
        run_server()
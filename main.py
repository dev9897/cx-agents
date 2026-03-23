"""
main.py
=======
THE SINGLE ENTRYPOINT for the SAP Commerce Shopping Agent.

Usage:
    python main.py                    # starts FastAPI server (default)
    python main.py --mode cli         # starts interactive CLI
    python main.py --mode server      # starts FastAPI server explicitly
    python main.py --mode check       # validates config & connectivity only
"""

import argparse
import logging
import os
import ssl
import sys

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Load .env FIRST before any other local imports
# ─────────────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Validate required environment variables before booting
# ─────────────────────────────────────────────────────────────────────────────
_provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
REQUIRED_ENV_VARS = [
    "SAP_BASE_URL",
    "SAP_SITE_ID",
    "SAP_CLIENT_ID",
    "SAP_CLIENT_SECRET",
    *(["GOOGLE_API_KEY"] if _provider == "gemini" else ["ANTHROPIC_API_KEY"]),
]

from app.middleware.logging_config import setup_logging

setup_logging()
logger = logging.getLogger("sap_agent.main")


def validate_env() -> bool:
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print("❌  Missing required environment variables:")
        for m in missing:
            print(f"      {m}")
        print("\n   Copy .env.example → .env and fill in the values.")
        return False
    print("✅  Environment variables OK")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Import local modules (after env is loaded)
# ─────────────────────────────────────────────────────────────────────────────
def _import_modules():
    """Deferred import so we can validate env before loading heavy deps."""
    global app_config, agent_service, app_main
    from app import config as _ac;  app_config = _ac
    from app.services import agent_service as _as;  agent_service = _as
    from app import main as _am;  app_main = _am


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Connectivity check
# ─────────────────────────────────────────────────────────────────────────────
def check_connectivity() -> bool:
    import httpx
    base = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
    site = os.getenv("SAP_SITE_ID", "")
    url  = f"{base}/{site}/catalogs"

    logger.info("🔍 SAP connectivity check → %s", url)
    logger.debug("   SAP_BASE_URL  = %s", base)
    logger.debug("   SAP_SITE_ID   = %s", site)

    # Log Python's active CA bundle so we know which certs are trusted
    try:
        ca_bundle = ssl.get_default_verify_paths()
        logger.debug("   SSL cafile    = %s", ca_bundle.cafile)
        logger.debug("   SSL capath    = %s", ca_bundle.capath)
        logger.debug("   SSL openssl   = %s", ssl.OPENSSL_VERSION)
    except Exception as ssl_info_err:
        logger.debug("   Could not read SSL paths: %s", ssl_info_err)

    try:
        r = httpx.get(url, timeout=10)
        if r.status_code < 500:
            logger.info("✅ SAP OCC API reachable (status=%d)", r.status_code)
            print(f"✅  SAP OCC API reachable ({r.status_code})")
            return True
        else:
            logger.warning("⚠️  SAP OCC API returned %d — check credentials", r.status_code)
            print(f"⚠️   SAP OCC API returned {r.status_code} — check credentials")
            return False

    except httpx.ConnectError as e:
        cause = str(e.__cause__ or e)
        if "CERTIFICATE_VERIFY_FAILED" in cause or "SSL" in cause.upper():
            logger.error(
                "🔒 SSL certificate verification failed hitting SAP OCC API\n"
                "   URL     : %s\n"
                "   Cause   : %s\n"
                "   Fix     : Add the SAP server's CA cert to your trust store,\n"
                "             or set SAP_SSL_VERIFY=false (dev only).",
                url, cause,
            )
        else:
            logger.error("❌ Connection error hitting SAP OCC API | url=%s | %s", url, cause)
        print(f"❌  Cannot reach SAP OCC API: {e}")
        return False

    except httpx.TimeoutException as e:
        logger.error("⏱️  Timeout reaching SAP OCC API | url=%s | %s", url, e)
        print(f"❌  Cannot reach SAP OCC API: {e}")
        return False

    except Exception as e:
        logger.exception("❌ Unexpected error during SAP connectivity check | url=%s", url)
        print(f"❌  Cannot reach SAP OCC API: {e}")
        return False


def check_anthropic() -> bool:
    import anthropic
    logger.info("🔍 Checking Anthropic API key...")
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        # lightweight model list call to verify key
        client.models.list()
        logger.info("✅ Anthropic API key valid")
        print("✅  Anthropic API key valid")
        return True
    except Exception as e:
        cause = str(e.__cause__ or e)
        if "CERTIFICATE_VERIFY_FAILED" in cause or "SSL" in cause.upper():
            logger.error(
                "🔒 SSL certificate error reaching Anthropic API\n"
                "   Cause: %s\n"
                "   Fix  : Run `pip install certifi` and set "
                "SSL_CERT_FILE=$(python -m certifi)",
                cause,
            )
        else:
            logger.error("❌ Anthropic API error: %s", e, exc_info=True)
        print(f"❌  Anthropic API error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MODE A: CLI interactive session
# ─────────────────────────────────────────────────────────────────────────────
def run_cli():
    from app.services.agent_service import get_last_ai_message, new_session, run_turn

    print("\n" + "="*60)
    print("  🛒  SAP Commerce Shopping Agent  —  CLI Mode")
    print("="*60)
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

        session_state = run_turn(user_input, thread_id, session_state)

        # Check if we hit a human-approval interrupt
        if session_state.get("__interrupt__"):
            interrupt_data = session_state["__interrupt__"][0].value
            print(f"\n  Confirmation required: {interrupt_data['message']}")
            answer = input("   Approve order? (yes/no): ").strip().lower()
            approved = answer in ("yes", "y")
            session_state = run_turn(
                "", thread_id, session_state,
                approval_response={"approved": approved},
            )

        reply = get_last_ai_message(session_state)
        if reply:
            print(f"\nAssistant: {reply}\n")

        # Show token usage every 5 turns
        turn = session_state.get("turn_count", 0)
        if turn > 0 and turn % 5 == 0:
            tin  = session_state.get("total_input_tokens", 0)
            tout = session_state.get("total_output_tokens", 0)
            print(f"  💡 [{turn} turns | tokens in={tin} out={tout}]\n")


# ─────────────────────────────────────────────────────────────────────────────
# MODE B: FastAPI server
# ─────────────────────────────────────────────────────────────────────────────
def run_server():
    import uvicorn
    from app.config import CONFIG

    # Cloud Run sets K_SERVICE; bind to 0.0.0.0 there, localhost locally
    is_cloud_run = os.getenv("K_SERVICE") is not None
    host = os.getenv("HOST", "0.0.0.0" if is_cloud_run else "127.0.0.1")
    port = int(os.getenv("PORT", "8080" if is_cloud_run else "8004"))
    reload = os.getenv("ENVIRONMENT", "production") == "development"

    print(f"\n  Starting SAP Commerce Agent API v2.0")
    print(f"    Host    : {host}:{port}")
    print(f"    Model   : {CONFIG.claude.model}")
    print(f"    Reload  : {reload}")
    print(f"    Docs    : http://localhost:{port}/docs\n")

    uvicorn.run(
        "app.main:app",
        host="localhost",
        port=port,
        reload=reload,
        log_level=CONFIG.observability.log_level.lower(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# MODE C: Config / connectivity check only
# ─────────────────────────────────────────────────────────────────────────────
def run_check():
    print("\n🔍  Running pre-flight checks...\n")
    env_ok  = validate_env()
    if not env_ok:
        sys.exit(1)
    sap_ok  = check_connectivity()
    anth_ok = check_anthropic()

    print()
    if env_ok and sap_ok and anth_ok:
        print("✅  All checks passed. Agent is ready to run.")
    else:
        print("❌  Some checks failed. Fix the issues above before starting.")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────
def main():
    """CLI entrypoint — used by `cxagent` command and `python main.py`."""
    parser = argparse.ArgumentParser(
        description="SAP Commerce Cloud Shopping Agent"
    )
    parser.add_argument(
        "--mode",
        choices=["server", "cli", "check"],
        default="server",
        help="server = FastAPI API, cli = interactive terminal, check = pre-flight only",
    )
    args = parser.parse_args()

    # Always validate env first
    if not validate_env():
        sys.exit(1)

    if args.mode == "check":
        run_check()

    elif args.mode == "cli":
        _import_modules()
        run_cli()

    else:  # server (default)
        _import_modules()
        run_server()


if __name__ == "__main__":
    main()
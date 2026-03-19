"""
FastAPI application factory.

Assembles routers, middleware, and shared state.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, chat, checkout, health, websocket
from app.agent.state import ShoppingState

# ── Shared session store ─────────────────────────────────────────────────────

_sessions: dict[str, ShoppingState] = {}


def create_app() -> FastAPI:
    app = FastAPI(title="SAP Commerce Shopping Agent", version="2.0.0")

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    # Inject shared session store into all routers
    auth.set_session_store(_sessions)
    chat.set_session_store(_sessions)
    checkout.set_session_store(_sessions)
    websocket.set_session_store(_sessions)

    # Register routers
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(checkout.router)
    app.include_router(websocket.router)

    # Try to load ACP router (optional)
    try:
        from acp.routes import router as acp_router
        app.include_router(acp_router)
    except ImportError:
        pass

    # Static files / UI
    _STATIC_DIR = Path(__file__).parent / "static"
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def root():
        # Try new static dir first, then legacy
        for static_dir in [_STATIC_DIR, Path(__file__).parent.parent / "static"]:
            index = static_dir / "index.html"
            if index.exists():
                return FileResponse(str(index), media_type="text/html")
        return HTMLResponse(
            "<h2>SAP Commerce Agent</h2>"
            "<p>See <a href='/docs'>/docs</a> for the API.</p>"
        )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        from fastapi.responses import Response
        return Response(status_code=204)

    return app


# Module-level app instance (used by uvicorn)
app = create_app()

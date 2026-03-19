"""WebSocket route for real-time streaming."""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.middleware.audit import audit
from app.services.agent_service import new_session, stream_turn

logger = logging.getLogger("sap_agent.api.ws")

router = APIRouter(tags=["WebSocket"])

_sessions: dict = {}


def set_session_store(sessions: dict) -> None:
    global _sessions
    _sessions = sessions


@router.websocket("/chat/stream")
async def chat_stream(ws: WebSocket):
    await ws.accept()
    state, thread_id = new_session()
    _sessions[thread_id] = state
    await ws.send_json({"type": "session", "session_id": thread_id})
    logger.info("ws | new session=%s", thread_id)

    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            user_message = payload.get("message", "")
            if not user_message:
                continue

            async for chunk in stream_turn(user_message, thread_id, _sessions[thread_id]):
                await ws.send_json({"type": "chunk", "text": chunk})
            await ws.send_json({"type": "done"})

    except WebSocketDisconnect:
        audit("WS_DISCONNECT", thread_id, {})
        logger.info("ws | disconnected | session=%s", thread_id)
    except Exception as exc:
        logger.exception("ws | error | session=%s", thread_id)
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close()

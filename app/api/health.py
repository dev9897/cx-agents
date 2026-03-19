"""Health check endpoint."""

from fastapi import APIRouter

from app.integrations import redis_client
from app.middleware.error_handler import sap_circuit_breaker

router = APIRouter(tags=["Health"])


@router.get("/health")
def health():
    return {
        "status": "ok",
        "circuit_breaker": "open" if sap_circuit_breaker.is_open else "closed",
        "redis": "connected" if redis_client.is_available() else "fallback",
    }

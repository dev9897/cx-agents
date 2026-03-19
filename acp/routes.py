"""
ACP REST endpoints — Agentic Commerce Protocol checkout API.

Routes:
  POST /acp/checkout_sessions              — Create a new checkout session
  POST /acp/checkout_sessions/{id}         — Update an existing session
  POST /acp/checkout_sessions/{id}/complete — Complete the checkout (place order)
  POST /acp/checkout_sessions/{id}/cancel  — Cancel a session
  GET  /acp/checkout_sessions/{id}         — Retrieve session state
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

from acp.models import (
    ACPError,
    CheckoutSessionResponse,
    CompleteCheckoutRequest,
    CreateCheckoutSessionRequest,
    ErrorType,
    UpdateCheckoutSessionRequest,
)
from acp.security import (
    ACP_API_VERSION,
    check_idempotency,
    store_idempotency,
    verify_api_key,
)
from acp.service import (
    cancel_checkout,
    complete_checkout,
    create_checkout_session,
    get_checkout_session,
    update_checkout_session,
)

logger = logging.getLogger("sap_agent.acp.routes")

router = APIRouter(prefix="/acp", tags=["Agentic Commerce Protocol"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _acp_headers(response: Response, request_id: Optional[str] = None) -> None:
    """Set standard ACP response headers."""
    response.headers["API-Version"] = ACP_API_VERSION
    if request_id:
        response.headers["Request-Id"] = request_id


def _get_request_metadata(request: Request) -> tuple[Optional[str], Optional[str]]:
    """Extract Idempotency-Key and Request-Id from headers."""
    return (
        request.headers.get("Idempotency-Key"),
        request.headers.get("Request-Id"),
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post(
    "/checkout_sessions",
    response_model=CheckoutSessionResponse,
    status_code=201,
    summary="Create a checkout session",
    description="Create a new ACP checkout session with items and optional buyer/address.",
)
async def create_session(
    req: CreateCheckoutSessionRequest,
    request: Request,
    response: Response,
):
    verify_api_key(request)
    idempotency_key, request_id = _get_request_metadata(request)
    _acp_headers(response, request_id)

    # Idempotency check
    cached = check_idempotency(idempotency_key)
    if cached:
        return cached

    if not req.items:
        raise HTTPException(
            status_code=400,
            detail=ACPError(
                type=ErrorType.INVALID_REQUEST,
                message="At least one item is required",
            ).model_dump(),
        )

    try:
        result = create_checkout_session(
            items=req.items,
            buyer=req.buyer,
            fulfillment_address=req.fulfillment_address,
        )
        store_idempotency(idempotency_key, result.model_dump())
        logger.info("ACP: POST /checkout_sessions → %s", result.id)
        return result
    except Exception as exc:
        logger.exception("ACP: create_session failed")
        raise HTTPException(
            status_code=500,
            detail=ACPError(
                type=ErrorType.PROCESSING_ERROR,
                message=str(exc),
            ).model_dump(),
        )


@router.post(
    "/checkout_sessions/{session_id}",
    response_model=CheckoutSessionResponse,
    summary="Update a checkout session",
    description="Update items, buyer, fulfillment address, or delivery option on an existing session.",
)
async def update_session(
    session_id: str,
    req: UpdateCheckoutSessionRequest,
    request: Request,
    response: Response,
):
    verify_api_key(request)
    idempotency_key, request_id = _get_request_metadata(request)
    _acp_headers(response, request_id)

    cached = check_idempotency(idempotency_key)
    if cached:
        return cached

    try:
        result = update_checkout_session(
            session_id=session_id,
            items=req.items,
            buyer=req.buyer,
            fulfillment_address=req.fulfillment_address,
            fulfillment_option_id=req.fulfillment_option_id,
        )
        store_idempotency(idempotency_key, result.model_dump())
        logger.info("ACP: POST /checkout_sessions/%s (update)", session_id)
        return result
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=ACPError(
                type=ErrorType.INVALID_REQUEST,
                message=str(exc),
            ).model_dump(),
        )
    except Exception as exc:
        logger.exception("ACP: update_session failed | session=%s", session_id)
        raise HTTPException(
            status_code=500,
            detail=ACPError(
                type=ErrorType.PROCESSING_ERROR,
                message=str(exc),
            ).model_dump(),
        )


@router.post(
    "/checkout_sessions/{session_id}/complete",
    response_model=CheckoutSessionResponse,
    summary="Complete checkout",
    description="Submit payment and place the order. Requires buyer info and a payment token.",
)
async def complete_session(
    session_id: str,
    req: CompleteCheckoutRequest,
    request: Request,
    response: Response,
):
    verify_api_key(request)
    idempotency_key, request_id = _get_request_metadata(request)
    _acp_headers(response, request_id)

    cached = check_idempotency(idempotency_key)
    if cached:
        return cached

    try:
        result = complete_checkout(
            session_id=session_id,
            buyer=req.buyer,
            payment_data=req.payment_data,
        )
        store_idempotency(idempotency_key, result.model_dump())
        logger.info("ACP: POST /checkout_sessions/%s/complete → order=%s",
                     session_id, result.order.id if result.order else "FAILED")
        return result
    except ValueError as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc).lower() else 400,
            detail=ACPError(
                type=ErrorType.INVALID_REQUEST,
                message=str(exc),
            ).model_dump(),
        )
    except Exception as exc:
        logger.exception("ACP: complete_session failed | session=%s", session_id)
        raise HTTPException(
            status_code=500,
            detail=ACPError(
                type=ErrorType.PROCESSING_ERROR,
                message=str(exc),
            ).model_dump(),
        )


@router.post(
    "/checkout_sessions/{session_id}/cancel",
    response_model=CheckoutSessionResponse,
    summary="Cancel a checkout session",
)
async def cancel_session(
    session_id: str,
    request: Request,
    response: Response,
):
    verify_api_key(request)
    _, request_id = _get_request_metadata(request)
    _acp_headers(response, request_id)

    try:
        result = cancel_checkout(session_id)
        logger.info("ACP: POST /checkout_sessions/%s/cancel", session_id)
        return result
    except ValueError as exc:
        raise HTTPException(
            status_code=404 if "not found" in str(exc).lower() else 400,
            detail=ACPError(
                type=ErrorType.INVALID_REQUEST,
                message=str(exc),
            ).model_dump(),
        )


@router.get(
    "/checkout_sessions/{session_id}",
    response_model=CheckoutSessionResponse,
    summary="Retrieve a checkout session",
)
async def retrieve_session(
    session_id: str,
    request: Request,
    response: Response,
):
    verify_api_key(request)
    _, request_id = _get_request_metadata(request)
    _acp_headers(response, request_id)

    try:
        result = get_checkout_session(session_id)
        return result
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=ACPError(
                type=ErrorType.INVALID_REQUEST,
                message=str(exc),
            ).model_dump(),
        )

"""
ACP Service — bridges Agentic Commerce Protocol operations to SAP Commerce OCC API.

Manages checkout sessions and maps between ACP and SAP data formats.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from acp.models import (
    ACPAddress,
    ACPMessage,
    ACPOrder,
    Buyer,
    CheckoutSessionResponse,
    CheckoutSessionStatus,
    ErrorCode,
    FulfillmentOption,
    FulfillmentType,
    Item,
    LineItem,
    Link,
    MessageType,
    OrderStatus,
    PaymentData,
    PaymentProvider,
    Total,
    TotalType,
)

load_dotenv()

logger = logging.getLogger("sap_agent.acp.service")


# ── SAP Commerce helpers (from refactored integration layer) ─────────────────

from app.integrations.sap_client import (
    _client,
    _headers,
    _handle_http_error,
    _safe_request,
    BASE_URL,
    SITE_ID,
)

import httpx

# Resolve the SAP access token for ACP requests
_SAP_TOKEN = os.getenv("SAP_STATIC_TOKEN", "")
_SAP_USER = os.getenv("SAP_STATIC_USERNAME", "current")
_CURRENCY = os.getenv("ACP_CURRENCY", "USD")
_STORE_URL = os.getenv("ACP_STORE_URL", "")


# ── Internal session store ───────────────────────────────────────────────────

class _ACPSession:
    """Internal state for an ACP checkout session."""

    def __init__(self, session_id: str):
        self.id = session_id
        self.status = CheckoutSessionStatus.NOT_READY_FOR_PAYMENT
        self.sap_cart_id: Optional[str] = None
        self.sap_user: str = _SAP_USER
        self.access_token: str = _SAP_TOKEN
        self.items: list[Item] = []
        self.buyer: Optional[Buyer] = None
        self.fulfillment_address: Optional[ACPAddress] = None
        self.selected_fulfillment_option_id: Optional[str] = None
        self.order: Optional[ACPOrder] = None
        self.messages: list[ACPMessage] = []
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        # Cache resolved line items from SAP
        self._line_items: list[LineItem] = []
        self._totals: list[Total] = []
        self._fulfillment_options: list[FulfillmentOption] = []


# In-memory store (swap with Redis for production)
_sessions: dict[str, _ACPSession] = {}


# ── SAP API wrappers (thin, no LangChain) ────────────────────────────────────

def _sap_create_cart(token: str, user: str) -> Optional[str]:
    """Create a SAP cart and return the cart code."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts"
    try:
        resp = _safe_request("POST", url, "acp_create_cart",
                             headers=_headers(token), json={})
        if resp.status_code in (200, 201):
            return resp.json().get("code")
    except httpx.HTTPError as e:
        logger.error("ACP: SAP create_cart failed: %s", e)
    return None


def _sap_add_to_cart(token: str, user: str, cart_id: str,
                     product_code: str, quantity: int) -> dict:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}/entries"
    try:
        resp = _safe_request("POST", url, "acp_add_to_cart",
                             headers=_headers(token),
                             json={"product": {"code": product_code}, "quantity": quantity})
        if resp.status_code in (200, 201):
            return resp.json()
    except httpx.HTTPError as e:
        logger.error("ACP: SAP add_to_cart failed: %s", e)
    return {}


def _sap_get_cart(token: str, user: str, cart_id: str) -> Optional[dict]:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}"
    try:
        resp = _safe_request("GET", url, "acp_get_cart",
                             params={"fields": "FULL"}, headers=_headers(token))
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError as e:
        logger.error("ACP: SAP get_cart failed: %s", e)
    return None


def _sap_delete_cart_entry(token: str, user: str, cart_id: str, entry_number: int) -> bool:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}/entries/{entry_number}"
    try:
        resp = _safe_request("DELETE", url, "acp_delete_entry", headers=_headers(token))
        return resp.status_code in (200, 204)
    except httpx.HTTPError:
        return False


def _sap_set_delivery_address(token: str, user: str, cart_id: str,
                               address: ACPAddress) -> bool:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}/addresses/delivery"
    # Split name into first/last
    parts = (address.name or "").split(" ", 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""
    payload = {
        "firstName":  first_name,
        "lastName":   last_name,
        "line1":      address.line_one or "",
        "line2":      address.line_two or "",
        "town":       address.city or "",
        "postalCode": address.postal_code or "",
        "country":    {"isocode": address.country or "US"},
    }
    try:
        resp = _safe_request("POST", url, "acp_set_address",
                             headers=_headers(token), json=payload)
        return resp.status_code in (200, 201)
    except httpx.HTTPError as e:
        logger.error("ACP: SAP set_delivery_address failed: %s", e)
        return False


def _sap_set_delivery_mode(token: str, user: str, cart_id: str,
                            mode_code: str) -> bool:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}/deliverymode"
    try:
        resp = _safe_request("PUT", url, "acp_set_delivery_mode",
                             headers=_headers(token),
                             params={"deliveryModeId": mode_code})
        return resp.status_code in (200, 204)
    except httpx.HTTPError as e:
        logger.error("ACP: SAP set_delivery_mode failed: %s", e)
        return False


def _sap_get_delivery_modes(token: str, user: str, cart_id: str) -> list[dict]:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}/deliverymodes"
    try:
        resp = _safe_request("GET", url, "acp_get_delivery_modes",
                             headers=_headers(token))
        if resp.status_code == 200:
            return resp.json().get("deliveryModes", [])
    except httpx.HTTPError as e:
        logger.error("ACP: SAP get_delivery_modes failed: %s", e)
    return []


def _sap_set_payment_details(token: str, user: str, cart_id: str,
                              payment_data: PaymentData,
                              buyer: Buyer) -> bool:
    """
    Set payment on SAP cart using the ACP Shared Payment Token.

    In a full Stripe integration, the token would be redeemed with Stripe
    to get actual card details. For now, we store the token reference as
    a placeholder payment on SAP.
    """
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/carts/{cart_id}/paymentdetails"
    billing = payment_data.billing_address or ACPAddress()
    b_parts = (billing.name or buyer.name or "").split(" ", 1)
    payload = {
        "accountHolderName": buyer.name or "",
        "cardNumber":        "4111111111111111",  # placeholder — real charge via PSP token
        "cardType":          {"code": "visa"},
        "expiryMonth":       "12",
        "expiryYear":        "2030",
        "cvn":               "123",
        "billingAddress": {
            "firstName":  b_parts[0] if b_parts else "",
            "lastName":   b_parts[1] if len(b_parts) > 1 else "",
            "line1":      billing.line_one or "",
            "town":       billing.city or "",
            "postalCode": billing.postal_code or "",
            "country":    {"isocode": billing.country or "US"},
        },
    }
    try:
        resp = _safe_request("POST", url, "acp_set_payment",
                             headers=_headers(token), json=payload)
        return resp.status_code in (200, 201)
    except httpx.HTTPError as e:
        logger.error("ACP: SAP set_payment_details failed: %s", e)
        return False


def _sap_place_order(token: str, user: str, cart_id: str) -> Optional[dict]:
    url = f"{BASE_URL}/{SITE_ID}/users/{user}/orders"
    try:
        resp = _safe_request("POST", url, "acp_place_order",
                             headers=_headers(token),
                             params={"cartId": cart_id, "fields": "FULL"})
        if resp.status_code in (200, 201):
            return resp.json()
    except httpx.HTTPError as e:
        logger.error("ACP: SAP place_order failed: %s", e)
    return None


def _sap_get_product(token: str, product_code: str) -> Optional[dict]:
    url = f"{BASE_URL}/{SITE_ID}/products/{product_code}"
    try:
        resp = _safe_request("GET", url, "acp_get_product",
                             params={"fields": "FULL"}, headers=_headers(token))
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError as e:
        logger.error("ACP: SAP get_product failed: %s", e)
    return None


# ── Currency helpers ─────────────────────────────────────────────────────────

def _to_cents(value) -> int:
    """Convert a SAP decimal price value to minor currency units (cents)."""
    if value is None:
        return 0
    try:
        return int(round(float(value) * 100))
    except (ValueError, TypeError):
        return 0


# ── Fulfillment option mapping ───────────────────────────────────────────────

_DELIVERY_MODE_MAP = {
    "standard-gross":  ("Standard Shipping", 3, 7),
    "standard-net":    ("Standard Shipping", 3, 7),
    "premium-gross":   ("Express Shipping", 1, 3),
    "premium-net":     ("Express Shipping", 1, 3),
    "free-standard":   ("Free Standard Shipping", 5, 10),
}


def _map_delivery_modes(sap_modes: list[dict]) -> list[FulfillmentOption]:
    """Convert SAP delivery modes to ACP fulfillment options."""
    options = []
    for mode in sap_modes:
        code = mode.get("code", "")
        name_hint = _DELIVERY_MODE_MAP.get(code, (mode.get("name", code), 3, 7))
        cost = _to_cents(mode.get("deliveryCost", {}).get("value", 0))
        options.append(FulfillmentOption(
            id=code,
            type=FulfillmentType.SHIPPING,
            name=name_hint[0],
            description=mode.get("description", ""),
            amount=cost,
            estimated_delivery_days_min=name_hint[1],
            estimated_delivery_days_max=name_hint[2],
        ))
    return options


# ── Session state computation ────────────────────────────────────────────────

def _compute_status(session: _ACPSession) -> CheckoutSessionStatus:
    """Determine ACP status from internal state."""
    if session.status in (CheckoutSessionStatus.COMPLETED, CheckoutSessionStatus.CANCELED):
        return session.status
    if session.status == CheckoutSessionStatus.IN_PROGRESS:
        return CheckoutSessionStatus.IN_PROGRESS

    # Ready for payment when: items + address + fulfillment option are set
    has_items = bool(session.items)
    has_address = session.fulfillment_address is not None
    has_fulfillment = session.selected_fulfillment_option_id is not None

    if has_items and has_address and has_fulfillment:
        return CheckoutSessionStatus.READY_FOR_PAYMENT
    return CheckoutSessionStatus.NOT_READY_FOR_PAYMENT


def _build_response(session: _ACPSession) -> CheckoutSessionResponse:
    """Build the ACP response from internal session state."""
    session.status = _compute_status(session)

    return CheckoutSessionResponse(
        id=session.id,
        status=session.status,
        currency=_CURRENCY,
        line_items=session._line_items,
        fulfillment_options=session._fulfillment_options,
        fulfillment_address=session.fulfillment_address,
        selected_fulfillment_option_id=session.selected_fulfillment_option_id,
        totals=session._totals,
        buyer=session.buyer,
        payment_provider=PaymentProvider(
            provider="stripe",
            supported_payment_methods=["card"],
        ),
        order=session.order,
        messages=session.messages,
        links=[],
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _refresh_cart_state(session: _ACPSession) -> None:
    """Fetch SAP cart and update line items + totals on the session."""
    if not session.sap_cart_id:
        return

    cart = _sap_get_cart(session.access_token, session.sap_user, session.sap_cart_id)
    if not cart:
        return

    # Line items
    line_items = []
    for entry in cart.get("entries", []):
        product = entry.get("product", {})
        base = _to_cents(entry.get("basePrice", {}).get("value"))
        total = _to_cents(entry.get("totalPrice", {}).get("value"))
        qty = entry.get("quantity", 1)
        line_items.append(LineItem(
            id=str(entry.get("entryNumber", 0)),
            item=Item(id=product.get("code", ""), quantity=qty),
            product_name=product.get("name", ""),
            product_description=product.get("summary", ""),
            base_amount=base * qty,
            subtotal=total,
            total=total,
        ))
    session._line_items = line_items

    # Totals
    totals = []
    sub_val = _to_cents(cart.get("subTotal", {}).get("value"))
    totals.append(Total(type=TotalType.SUBTOTAL, amount=sub_val, label="Subtotal"))

    delivery_cost = _to_cents(cart.get("deliveryCost", {}).get("value"))
    if delivery_cost:
        totals.append(Total(type=TotalType.FULFILLMENT, amount=delivery_cost, label="Shipping"))

    tax_val = _to_cents(cart.get("totalTax", {}).get("value"))
    if tax_val:
        totals.append(Total(type=TotalType.TAX, amount=tax_val, label="Tax"))

    total_val = _to_cents(cart.get("totalPrice", {}).get("value"))
    totals.append(Total(type=TotalType.TOTAL, amount=total_val, label="Total"))

    discount_val = _to_cents(cart.get("totalDiscounts", {}).get("value"))
    if discount_val:
        totals.append(Total(type=TotalType.DISCOUNT, amount=discount_val, label="Discount"))

    session._totals = totals

    # Fulfillment options
    sap_modes = _sap_get_delivery_modes(
        session.access_token, session.sap_user, session.sap_cart_id
    )
    session._fulfillment_options = _map_delivery_modes(sap_modes)

    session.updated_at = datetime.now(timezone.utc)


# ── Public Service API ───────────────────────────────────────────────────────

def create_checkout_session(
    items: list[Item],
    buyer: Optional[Buyer] = None,
    fulfillment_address: Optional[ACPAddress] = None,
) -> CheckoutSessionResponse:
    """
    ACP: Create a new checkout session.
    Maps to: SAP create_cart + add_to_cart for each item.
    """
    session_id = f"cs_{uuid.uuid4().hex[:24]}"
    session = _ACPSession(session_id)
    session.buyer = buyer
    session.items = items
    session.messages = []

    # Create SAP cart
    cart_id = _sap_create_cart(session.access_token, session.sap_user)
    if not cart_id:
        session.messages.append(ACPMessage(
            type=MessageType.ERROR,
            code=ErrorCode.INVALID,
            message="Failed to create shopping cart",
        ))
        _sessions[session_id] = session
        return _build_response(session)

    session.sap_cart_id = cart_id
    logger.info("ACP: created session %s → SAP cart %s", session_id, cart_id)

    # Add items to SAP cart
    for item in items:
        result = _sap_add_to_cart(
            session.access_token, session.sap_user, cart_id,
            item.id, item.quantity,
        )
        if not result:
            session.messages.append(ACPMessage(
                type=MessageType.ERROR,
                code=ErrorCode.OUT_OF_STOCK,
                message=f"Could not add item {item.id}",
                param=f"$.items[?(@.id=='{item.id}')]",
            ))

    # Set fulfillment address if provided
    if fulfillment_address:
        session.fulfillment_address = fulfillment_address
        ok = _sap_set_delivery_address(
            session.access_token, session.sap_user, cart_id, fulfillment_address,
        )
        if not ok:
            session.messages.append(ACPMessage(
                type=MessageType.ERROR,
                code=ErrorCode.INVALID,
                message="Failed to set delivery address",
                param="$.fulfillment_address",
            ))

    # Refresh from SAP to get line items, totals, delivery modes
    _refresh_cart_state(session)
    _sessions[session_id] = session

    return _build_response(session)


def update_checkout_session(
    session_id: str,
    items: Optional[list[Item]] = None,
    buyer: Optional[Buyer] = None,
    fulfillment_address: Optional[ACPAddress] = None,
    fulfillment_option_id: Optional[str] = None,
) -> CheckoutSessionResponse:
    """
    ACP: Update an existing checkout session.
    Recalculates the full cart state after each mutation.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session.status in (CheckoutSessionStatus.COMPLETED, CheckoutSessionStatus.CANCELED):
        raise ValueError(f"Session {session_id} is {session.status.value}")

    session.messages = []

    # Update buyer
    if buyer:
        session.buyer = buyer

    # Update items — clear cart and re-add
    if items is not None:
        session.items = items
        if session.sap_cart_id:
            # Get current entries and delete them
            cart = _sap_get_cart(session.access_token, session.sap_user, session.sap_cart_id)
            if cart:
                for entry in cart.get("entries", []):
                    _sap_delete_cart_entry(
                        session.access_token, session.sap_user,
                        session.sap_cart_id, entry["entryNumber"],
                    )
            # Re-add new items
            for item in items:
                result = _sap_add_to_cart(
                    session.access_token, session.sap_user,
                    session.sap_cart_id, item.id, item.quantity,
                )
                if not result:
                    session.messages.append(ACPMessage(
                        type=MessageType.ERROR,
                        code=ErrorCode.OUT_OF_STOCK,
                        message=f"Could not add item {item.id}",
                        param=f"$.items[?(@.id=='{item.id}')]",
                    ))

    # Update fulfillment address
    if fulfillment_address:
        session.fulfillment_address = fulfillment_address
        if session.sap_cart_id:
            ok = _sap_set_delivery_address(
                session.access_token, session.sap_user,
                session.sap_cart_id, fulfillment_address,
            )
            if not ok:
                session.messages.append(ACPMessage(
                    type=MessageType.ERROR,
                    code=ErrorCode.INVALID,
                    message="Failed to set delivery address",
                    param="$.fulfillment_address",
                ))

    # Set fulfillment option (delivery mode)
    if fulfillment_option_id:
        session.selected_fulfillment_option_id = fulfillment_option_id
        if session.sap_cart_id:
            ok = _sap_set_delivery_mode(
                session.access_token, session.sap_user,
                session.sap_cart_id, fulfillment_option_id,
            )
            if not ok:
                session.messages.append(ACPMessage(
                    type=MessageType.ERROR,
                    code=ErrorCode.INVALID,
                    message=f"Invalid fulfillment option: {fulfillment_option_id}",
                    param="$.fulfillment_option_id",
                ))

    _refresh_cart_state(session)
    return _build_response(session)


def complete_checkout(
    session_id: str,
    buyer: Buyer,
    payment_data: PaymentData,
) -> CheckoutSessionResponse:
    """
    ACP: Complete the checkout — charge the buyer and place the order.
    Maps to: SAP set_payment_details + place_order.
    """
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session.status == CheckoutSessionStatus.COMPLETED:
        raise ValueError(f"Session {session_id} is already completed")
    if session.status == CheckoutSessionStatus.CANCELED:
        raise ValueError(f"Session {session_id} is canceled")

    session.messages = []
    session.buyer = buyer
    session.status = CheckoutSessionStatus.IN_PROGRESS

    # Set payment details on SAP cart
    if not session.sap_cart_id:
        session.messages.append(ACPMessage(
            type=MessageType.ERROR,
            code=ErrorCode.INVALID,
            message="No cart associated with this session",
        ))
        return _build_response(session)

    ok = _sap_set_payment_details(
        session.access_token, session.sap_user,
        session.sap_cart_id, payment_data, buyer,
    )
    if not ok:
        session.messages.append(ACPMessage(
            type=MessageType.ERROR,
            code=ErrorCode.PAYMENT_DECLINED,
            message="Failed to set payment details",
        ))
        session.status = CheckoutSessionStatus.READY_FOR_PAYMENT
        return _build_response(session)

    # Place the order
    order_data = _sap_place_order(
        session.access_token, session.sap_user, session.sap_cart_id,
    )
    if not order_data:
        session.messages.append(ACPMessage(
            type=MessageType.ERROR,
            code=ErrorCode.PAYMENT_DECLINED,
            message="Order placement failed",
        ))
        session.status = CheckoutSessionStatus.READY_FOR_PAYMENT
        return _build_response(session)

    order_code = order_data.get("code", "")
    session.order = ACPOrder(
        id=order_code,
        checkout_session_id=session_id,
        status=OrderStatus.CREATED,
        permalink_url=f"{_STORE_URL}/my-account/order/{order_code}" if _STORE_URL else None,
    )
    session.status = CheckoutSessionStatus.COMPLETED

    # Refresh totals from the final cart/order state
    _refresh_cart_state(session)

    logger.info("ACP: session %s completed → order %s", session_id, order_code)
    return _build_response(session)


def cancel_checkout(session_id: str) -> CheckoutSessionResponse:
    """ACP: Cancel a checkout session."""
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    if session.status == CheckoutSessionStatus.COMPLETED:
        raise ValueError(f"Session {session_id} is already completed — cannot cancel")

    session.status = CheckoutSessionStatus.CANCELED
    session.messages = []
    session.updated_at = datetime.now(timezone.utc)

    logger.info("ACP: session %s canceled", session_id)
    return _build_response(session)


def get_checkout_session(session_id: str) -> CheckoutSessionResponse:
    """ACP: Retrieve the current state of a checkout session."""
    session = _sessions.get(session_id)
    if not session:
        raise ValueError(f"Session {session_id} not found")

    # Refresh live data if still active
    if session.status not in (CheckoutSessionStatus.COMPLETED, CheckoutSessionStatus.CANCELED):
        _refresh_cart_state(session)

    return _build_response(session)

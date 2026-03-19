"""Session and commerce state models."""

from enum import Enum
from typing import Optional, TypedDict


class CommerceState(str, Enum):
    ANONYMOUS = "ANONYMOUS"
    AUTHENTICATED = "AUTHENTICATED"
    CART_CREATED = "CART_CREATED"
    ITEMS_ADDED = "ITEMS_ADDED"
    ADDRESS_SET = "ADDRESS_SET"
    DELIVERY_MODE_SET = "DELIVERY_MODE_SET"
    PAYMENT_SET = "PAYMENT_SET"
    READY_FOR_ORDER = "READY_FOR_ORDER"
    ORDER_PLACED = "ORDER_PLACED"
    # Stripe checkout states
    CHECKOUT_INITIATED = "CHECKOUT_INITIATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAYMENT_COMPLETED = "PAYMENT_COMPLETED"
    ORDER_PLACING = "ORDER_PLACING"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_FAILED = "ORDER_FAILED"


class SessionState(TypedDict):
    session_id: str
    user_id: str
    access_token: Optional[str]
    cart_id: Optional[str]
    commerce_state: CommerceState
    intent: Optional[str]


ALLOWED_TRANSITIONS = {
    CommerceState.ANONYMOUS: [CommerceState.AUTHENTICATED],
    CommerceState.AUTHENTICATED: [CommerceState.CART_CREATED],
    CommerceState.CART_CREATED: [CommerceState.ITEMS_ADDED],
    CommerceState.ITEMS_ADDED: [CommerceState.ADDRESS_SET],
    CommerceState.ADDRESS_SET: [CommerceState.DELIVERY_MODE_SET],
    CommerceState.DELIVERY_MODE_SET: [CommerceState.PAYMENT_SET, CommerceState.CHECKOUT_INITIATED],
    CommerceState.PAYMENT_SET: [CommerceState.READY_FOR_ORDER],
    CommerceState.READY_FOR_ORDER: [CommerceState.ORDER_PLACED],
    # Stripe checkout flow
    CommerceState.CHECKOUT_INITIATED: [CommerceState.PAYMENT_PENDING],
    CommerceState.PAYMENT_PENDING: [CommerceState.PAYMENT_COMPLETED, CommerceState.ORDER_FAILED],
    CommerceState.PAYMENT_COMPLETED: [CommerceState.ORDER_PLACING],
    CommerceState.ORDER_PLACING: [CommerceState.ORDER_CONFIRMED, CommerceState.ORDER_FAILED],
}


def can_transition(current: CommerceState, target: CommerceState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, [])

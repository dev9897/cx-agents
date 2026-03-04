from typing import Optional, TypedDict
from enum import Enum


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


class SessionState(TypedDict):
    session_id: str
    user_id: str
    access_token: Optional[str]
    cart_id: Optional[str]
    commerce_state: CommerceState
    intent: Optional[str]
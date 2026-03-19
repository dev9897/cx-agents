"""Checkout and payment models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CheckoutStatus(str, Enum):
    PENDING = "pending"
    PAYMENT_CREATED = "payment_created"
    PAYMENT_COMPLETED = "payment_completed"
    ORDER_PLACED = "order_placed"
    FAILED = "failed"
    CANCELED = "canceled"


class CartItem(BaseModel):
    product_code: str
    product_name: str = ""
    quantity: int = 1
    unit_price: float = 0.0
    total_price: float = 0.0
    image_url: Optional[str] = None


class CartSummary(BaseModel):
    cart_id: str
    items: list[CartItem] = Field(default_factory=list)
    subtotal: float = 0.0
    shipping_cost: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    currency: str = "USD"


class DeliveryAddress(BaseModel):
    first_name: str = ""
    last_name: str = ""
    line1: str = ""
    line2: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = "US"


class CheckoutSession(BaseModel):
    """Tracks the full lifecycle of a checkout attempt."""
    id: str
    session_id: str
    cart_id: str
    status: CheckoutStatus = CheckoutStatus.PENDING
    cart_summary: Optional[CartSummary] = None
    delivery_address: Optional[DeliveryAddress] = None
    delivery_mode: str = "standard-gross"
    stripe_session_id: Optional[str] = None
    stripe_payment_url: Optional[str] = None
    order_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    retry_count: int = 0


class OrderRecord(BaseModel):
    """Stored order for history and reorder."""
    order_code: str
    session_id: str
    items: list[CartItem] = Field(default_factory=list)
    total: float = 0.0
    currency: str = "USD"
    delivery_address: Optional[DeliveryAddress] = None
    delivery_mode: str = ""
    status: str = ""
    created_at: Optional[datetime] = None

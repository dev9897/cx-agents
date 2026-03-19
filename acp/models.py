"""
ACP data models — Pydantic schemas matching the Agentic Commerce Protocol spec.

All monetary amounts are in **minor currency units** (cents).
Spec version: 2026-01-30
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────────

class CheckoutSessionStatus(str, Enum):
    NOT_READY_FOR_PAYMENT = "not_ready_for_payment"
    READY_FOR_PAYMENT = "ready_for_payment"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELED = "canceled"


class OrderStatus(str, Enum):
    CREATED = "created"
    MANUAL_REVIEW = "manual_review"
    CONFIRMED = "confirmed"
    CANCELED = "canceled"
    SHIPPED = "shipped"
    FULFILLED = "fulfilled"


class FulfillmentType(str, Enum):
    SHIPPING = "shipping"
    DIGITAL = "digital"


class TotalType(str, Enum):
    ITEMS_BASE_AMOUNT = "items_base_amount"
    ITEMS_DISCOUNT = "items_discount"
    SUBTOTAL = "subtotal"
    DISCOUNT = "discount"
    FULFILLMENT = "fulfillment"
    TAX = "tax"
    FEE = "fee"
    TOTAL = "total"


class MessageType(str, Enum):
    INFO = "info"
    ERROR = "error"


class ErrorCode(str, Enum):
    MISSING = "missing"
    INVALID = "invalid"
    OUT_OF_STOCK = "out_of_stock"
    PAYMENT_DECLINED = "payment_declined"
    REQUIRES_SIGN_IN = "requires_sign_in"
    REQUIRES_3DS = "requires_3ds"


class ErrorType(str, Enum):
    INVALID_REQUEST = "invalid_request"
    REQUEST_NOT_IDEMPOTENT = "request_not_idempotent"
    PROCESSING_ERROR = "processing_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


# ── Core Models ──────────────────────────────────────────────────────────────

class ACPAddress(BaseModel):
    name: Optional[str] = None
    line_one: Optional[str] = None
    line_two: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None          # ISO 3166-1 alpha-2
    postal_code: Optional[str] = None
    phone_number: Optional[str] = None


class Buyer(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None


class Item(BaseModel):
    """Item reference in a create/update request."""
    id: str
    quantity: int = 1


class LineItem(BaseModel):
    """Resolved line item in a checkout session response."""
    id: str
    item: Item
    product_name: Optional[str] = None
    product_description: Optional[str] = None
    product_image_url: Optional[str] = None
    base_amount: int = 0          # cents
    discount: int = 0             # cents
    subtotal: int = 0             # cents
    tax: int = 0                  # cents
    total: int = 0                # cents


class FulfillmentOption(BaseModel):
    id: str
    type: FulfillmentType = FulfillmentType.SHIPPING
    name: Optional[str] = None
    description: Optional[str] = None
    amount: int = 0               # cents
    estimated_delivery_days_min: Optional[int] = None
    estimated_delivery_days_max: Optional[int] = None


class Total(BaseModel):
    type: TotalType
    amount: int = 0               # cents
    label: Optional[str] = None


class PaymentProvider(BaseModel):
    provider: str                 # "stripe", "adyen", etc.
    supported_payment_methods: list[str] = Field(default_factory=lambda: ["card"])


class PaymentData(BaseModel):
    token: str
    provider: str
    billing_address: Optional[ACPAddress] = None


class ACPMessage(BaseModel):
    type: MessageType
    code: Optional[ErrorCode] = None
    message: str
    param: Optional[str] = None   # JSONPath reference


class Link(BaseModel):
    rel: str
    href: str


class ACPOrder(BaseModel):
    id: str
    checkout_session_id: str
    status: OrderStatus = OrderStatus.CREATED
    permalink_url: Optional[str] = None


class ACPError(BaseModel):
    type: ErrorType
    message: str


# ── Request / Response Models ────────────────────────────────────────────────

class CreateCheckoutSessionRequest(BaseModel):
    items: list[Item]
    buyer: Optional[Buyer] = None
    fulfillment_address: Optional[ACPAddress] = None


class UpdateCheckoutSessionRequest(BaseModel):
    items: Optional[list[Item]] = None
    buyer: Optional[Buyer] = None
    fulfillment_address: Optional[ACPAddress] = None
    fulfillment_option_id: Optional[str] = None


class CompleteCheckoutRequest(BaseModel):
    buyer: Buyer
    payment_data: PaymentData


class CheckoutSessionResponse(BaseModel):
    id: str
    status: CheckoutSessionStatus
    currency: str = "USD"
    line_items: list[LineItem] = Field(default_factory=list)
    fulfillment_options: list[FulfillmentOption] = Field(default_factory=list)
    fulfillment_address: Optional[ACPAddress] = None
    selected_fulfillment_option_id: Optional[str] = None
    totals: list[Total] = Field(default_factory=list)
    buyer: Optional[Buyer] = None
    payment_provider: Optional[PaymentProvider] = None
    order: Optional[ACPOrder] = None
    messages: list[ACPMessage] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── Webhook Models ───────────────────────────────────────────────────────────

class WebhookEvent(BaseModel):
    id: str
    type: str                    # "order_created", "order_updated"
    data: dict
    created_at: datetime

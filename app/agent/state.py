"""Agent state definition — the typed dict that flows through the graph."""

from typing import Annotated, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ShoppingState(TypedDict):
    # Core conversation
    messages: Annotated[list[BaseMessage], add_messages]

    # SAP session
    access_token: Optional[str]
    user_id: str                  # "current" | "anonymous"
    cart_id: Optional[str]
    order_code: Optional[str]
    username: Optional[str]
    user_email: Optional[str]
    mcp_session_id: Optional[str]

    # Stripe checkout
    stripe_checkout_session_id: Optional[str]
    stripe_payment_url: Optional[str]
    checkout_status: Optional[str]

    # ACP / saved cards
    stripe_customer_id: Optional[str]
    saved_payment_methods: Optional[list[dict]]

    # SAP user profile (fetched at login)
    saved_addresses: Optional[list[dict]]
    sap_payment_details: Optional[list[dict]]

    # Observability / cost
    session_id: str
    total_input_tokens: int
    total_output_tokens: int
    turn_count: int

    # Structured data from tools for frontend rendering
    last_search_results: Optional[list[dict]]
    last_cart_data: Optional[dict]
    last_product_detail: Optional[dict]

    # Error handling
    last_error: Optional[str]
    consecutive_errors: int
    rejected_tool_calls: Optional[list[str]]

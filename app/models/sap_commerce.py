"""Strict Pydantic models for SAP Commerce Cloud data.

All SAP OCC API responses are validated through these models to ensure
consistent shape and catch breaking API changes early.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

_HTML_TAG_RE = re.compile(r"<[^>]+>")


# ── Product ──────────────────────────────────────────────────────────────────

class ProductCard(BaseModel):
    """Product data for frontend display cards."""

    code: str
    name: str
    price: str = "N/A"
    price_value: Optional[float] = None
    stock: str = "unknown"
    rating: Optional[float] = None
    summary: str = ""
    image_url: Optional[str] = None
    categories: list[str] = Field(default_factory=list)

    @staticmethod
    def from_sap_product(p: dict, base_media_url: str = "") -> ProductCard:
        """Parse a raw SAP OCC product dict into a ProductCard."""
        return ProductCard(
            code=p.get("code", ""),
            name=strip_html(p.get("name", "")),
            price=p.get("price", {}).get("formattedValue", "N/A"),
            price_value=p.get("price", {}).get("value"),
            stock=p.get("stock", {}).get("stockLevelStatus", "unknown"),
            rating=p.get("averageRating"),
            summary=strip_html(p.get("summary", "")),
            image_url=extract_image_url(p.get("images", []), base_media_url),
            categories=[c.get("name", "") for c in p.get("categories", [])],
        )

    def to_tool_dict(self) -> dict:
        """Serialize for MCP tool / API response."""
        return {
            "code": self.code,
            "name": self.name,
            "price": self.price,
            "priceValue": self.price_value,
            "stock": self.stock,
            "rating": self.rating,
            "summary": self.summary,
            "image_url": self.image_url,
        }


# ── Cart ─────────────────────────────────────────────────────────────────────

class CartEntryCard(BaseModel):
    """Cart entry for frontend display."""

    entry_number: int
    product_code: str
    product_name: str
    quantity: int = 1
    base_price: Optional[str] = None
    base_price_value: Optional[float] = None
    total: Optional[str] = None
    total_value: Optional[float] = None
    image_url: Optional[str] = None

    @staticmethod
    def from_sap_entry(e: dict, base_media_url: str = "") -> CartEntryCard:
        """Parse a raw SAP OCC cart entry dict."""
        product = e.get("product", {})
        return CartEntryCard(
            entry_number=e.get("entryNumber", 0),
            product_code=product.get("code", ""),
            product_name=strip_html(product.get("name", "")),
            quantity=e.get("quantity", 1),
            base_price=e.get("basePrice", {}).get("formattedValue"),
            base_price_value=e.get("basePrice", {}).get("value"),
            total=e.get("totalPrice", {}).get("formattedValue"),
            total_value=e.get("totalPrice", {}).get("value"),
            image_url=extract_image_url(product.get("images", []), base_media_url),
        )


class CartCard(BaseModel):
    """Full cart data for frontend display."""

    cart_id: str
    entries: list[CartEntryCard] = Field(default_factory=list)
    item_count: int = 0
    sub_total: Optional[str] = None
    sub_total_value: Optional[float] = None
    delivery_cost: Optional[str] = None
    total_tax: Optional[str] = None
    total: Optional[str] = None
    total_value: Optional[float] = None
    currency: str = "USD"

    @staticmethod
    def from_sap_cart(d: dict, base_media_url: str = "") -> CartCard:
        """Parse a raw SAP OCC cart response dict."""
        return CartCard(
            cart_id=d.get("code", ""),
            entries=[
                CartEntryCard.from_sap_entry(e, base_media_url)
                for e in d.get("entries", [])
            ],
            item_count=d.get("totalItems", 0),
            sub_total=d.get("subTotal", {}).get("formattedValue"),
            sub_total_value=d.get("subTotal", {}).get("value"),
            delivery_cost=d.get("deliveryCost", {}).get("formattedValue"),
            total_tax=d.get("totalTax", {}).get("formattedValue"),
            total=d.get("totalPrice", {}).get("formattedValue"),
            total_value=d.get("totalPrice", {}).get("value"),
            currency=d.get("totalPrice", {}).get("currencyIso", "USD"),
        )

    def to_tool_dict(self) -> dict:
        """Serialize for MCP tool / API response."""
        return {
            "success": True,
            "cart_id": self.cart_id,
            "total": self.total,
            "totalValue": self.total_value,
            "subTotal": self.sub_total,
            "subTotalValue": self.sub_total_value,
            "deliveryCost": self.delivery_cost,
            "totalTax": self.total_tax,
            "currency": self.currency,
            "item_count": self.item_count,
            "entries": [
                {
                    "entry_number": e.entry_number,
                    "product_code": e.product_code,
                    "product_name": e.product_name,
                    "quantity": e.quantity,
                    "base_price": e.base_price,
                    "base_price_value": e.base_price_value,
                    "total": e.total,
                    "total_value": e.total_value,
                    "image_url": e.image_url,
                }
                for e in self.entries
            ],
        }


# ── Order ────────────────────────────────────────────────────────────────────

class OrderResult(BaseModel):
    """Order placement result."""

    success: bool = True
    order_code: str = ""
    status: str = ""
    total: Optional[str] = None
    total_value: Optional[float] = None
    created: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    """Remove HTML tags from SAP response fields (e.g. <em class='search-results-highlight'>)."""
    if not text:
        return ""
    return _HTML_TAG_RE.sub("", text).strip()


def extract_image_url(images: list, base_media_url: str = "") -> Optional[str]:
    """Extract best product image URL from SAP images array.

    Prefers 'product' format (300px), then 'thumbnail' (96px), then first.
    Resolves relative URLs using base_media_url.
    """
    if not images:
        return None
    for preferred_format in ("product", "thumbnail"):
        for img in images:
            if img.get("format") == preferred_format and img.get("url"):
                return _resolve_url(img["url"], base_media_url)
    first = images[0]
    if first.get("url"):
        return _resolve_url(first["url"], base_media_url)
    return None


def _resolve_url(url: str, base: str) -> str:
    """Resolve a potentially relative URL against a base."""
    if url.startswith(("http://", "https://")):
        return url
    return f"{base.rstrip('/')}{url}" if base else url


def get_base_media_url(base_url: str) -> str:
    """Derive SAP media base URL from OCC API URL.

    Example: 'https://sap.example.com/occ/v2' → 'https://sap.example.com'
    """
    return base_url.split("/occ/v2")[0] if "/occ/v2" in base_url else base_url

"""
Storefront API — product listing, search, detail, and cart for the e-commerce UI.

These endpoints call SAP Commerce OCC directly for frontend rendering,
separate from the agent chat flow.
"""

import logging
import os

import httpx
from fastapi import APIRouter, Query

logger = logging.getLogger("sap_agent.storefront")

router = APIRouter(prefix="/store", tags=["Storefront"])

_BASE_URL = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
_SITE_ID = os.getenv("SAP_SITE_ID", "electronics")


def _sap_get(path: str, params: dict = None) -> dict:
    """Helper to make a GET request to SAP OCC."""
    url = f"{_BASE_URL}/{_SITE_ID}{path}"
    try:
        r = httpx.get(url, params=params, timeout=10, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("SAP OCC request failed: %s %s", url, e)
        return {}


@router.get("/products")
def search_products(
    q: str = Query("", description="Search query"),
    page: int = Query(0, ge=0),
    page_size: int = Query(20, ge=1, le=50),
    sort: str = Query("relevance", description="Sort order"),
):
    """Search products from SAP Commerce catalog."""
    params = {
        "query": q or "*:*",
        "currentPage": page,
        "pageSize": page_size,
        "sort": sort,
        "fields": "FULL",
    }
    data = _sap_get("/products/search", params)
    base_media = _BASE_URL.replace("/occ/v2", "")

    products = []
    for p in data.get("products", []):
        # Extract best primary image — prefer largest format first
        image_url = ""
        for fmt in ("zoom", "product", "thumbnail"):
            for img in p.get("images", []):
                if img.get("imageType") == "PRIMARY" and img.get("format") == fmt:
                    url_path = img.get("url", "")
                    if url_path:
                        image_url = base_media + url_path if url_path.startswith("/") else url_path
                        break
            if image_url:
                break

        price = p.get("price", {})
        stock = p.get("stock", {})
        products.append({
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "price": price.get("formattedValue", "N/A") if isinstance(price, dict) else "N/A",
            "priceValue": price.get("value", 0) if isinstance(price, dict) else 0,
            "currency": price.get("currencyIso", "USD") if isinstance(price, dict) else "USD",
            "image": image_url,
            "stock": stock.get("stockLevelStatus", "unknown") if isinstance(stock, dict) else "unknown",
            "summary": (p.get("summary") or "")[:200],
            "averageRating": p.get("averageRating", 0),
            "numberOfReviews": p.get("numberOfReviews", 0),
        })

    pagination = data.get("pagination", {})
    return {
        "products": products,
        "pagination": {
            "currentPage": pagination.get("currentPage", 0),
            "totalPages": pagination.get("totalPages", 0),
            "totalResults": pagination.get("totalResults", 0),
            "pageSize": pagination.get("pageSize", page_size),
        },
        "sorts": [s.get("code", "") for s in data.get("sorts", [])],
    }


@router.get("/products/{code}")
def get_product(code: str):
    """Get full product details."""
    data = _sap_get(f"/products/{code}", {"fields": "FULL"})
    if not data:
        return {"error": "Product not found"}

    base_media = _BASE_URL.replace("/occ/v2", "")
    images = []
    for img in data.get("images", []):
        url_path = img.get("url", "")
        if url_path:
            images.append({
                "url": base_media + url_path if url_path.startswith("/") else url_path,
                "format": img.get("format", ""),
                "imageType": img.get("imageType", ""),
            })

    price = data.get("price", {})
    stock = data.get("stock", {})
    return {
        "code": data.get("code", ""),
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "summary": data.get("summary", ""),
        "price": price.get("formattedValue", "N/A") if isinstance(price, dict) else "N/A",
        "priceValue": price.get("value", 0) if isinstance(price, dict) else 0,
        "images": images,
        "stock": stock.get("stockLevelStatus", "unknown") if isinstance(stock, dict) else "unknown",
        "stockLevel": stock.get("stockLevel", 0) if isinstance(stock, dict) else 0,
        "averageRating": data.get("averageRating", 0),
        "numberOfReviews": data.get("numberOfReviews", 0),
        "categories": [c.get("name", "") for c in data.get("categories", []) if isinstance(c, dict)],
        "classifications": data.get("classifications", []),
    }


@router.get("/categories")
def get_categories():
    """Get top-level catalog categories."""
    data = _sap_get("/catalogs/electronicsProductCatalog/Online", {"fields": "FULL"})
    categories = []
    for cat in data.get("categories", data.get("catalogCategories", [])):
        categories.append({
            "id": cat.get("id", ""),
            "name": cat.get("name", ""),
            "subcategories": [
                {"id": sub.get("id", ""), "name": sub.get("name", "")}
                for sub in cat.get("subcategories", [])[:10]
            ]
        })
    return {"categories": categories}

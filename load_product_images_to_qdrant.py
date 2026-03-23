"""
load_product_images_to_qdrant.py
================================
Fetch product images from SAP Commerce and index them into
the Qdrant CLIP collection for visual/image search.

Run: python load_product_images_to_qdrant.py

Prerequisites:
  pip install cxagent[vision,search]
  - QDRANT_HOST and QDRANT_API_KEY set in .env
  - SAP Commerce running with product images
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

SAP_URL = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
SITE_ID = os.getenv("SAP_SITE_ID", "electronics")
QDRANT_HOST = os.getenv("QDRANT_HOST")

if not QDRANT_HOST:
    print("ERROR: QDRANT_HOST not set in .env")
    sys.exit(1)


def fetch_products_with_images(queries=None, max_pages=5):
    """Fetch products from SAP OCC that have images."""
    import httpx

    if queries is None:
        queries = ["camera", "lens", "memory card", "tripod", "battery"]

    http = httpx.Client(timeout=30, verify=False)
    all_products = []
    seen = set()
    base_media = SAP_URL.replace("/occ/v2", "")

    for query in queries:
        print(f"\n  Fetching: '{query}'")
        for page in range(max_pages):
            url = f"{SAP_URL}/{SITE_ID}/products/search"
            params = {"query": query, "pageSize": 20, "currentPage": page, "fields": "FULL"}
            resp = http.get(url, params=params)
            if resp.status_code != 200:
                break

            data = resp.json()
            products = data.get("products", [])
            if not products:
                break

            for p in products:
                code = p.get("code", "")
                if code in seen:
                    continue
                seen.add(code)

                # Extract image URL
                images = p.get("images", [])
                image_url = ""
                for img in images:
                    if img.get("imageType") == "PRIMARY" and img.get("format") in ("product", "thumbnail", "cartIcon"):
                        url_path = img.get("url", "")
                        if url_path:
                            image_url = base_media + url_path if url_path.startswith("/") else url_path
                            break

                if not image_url:
                    continue

                price = p.get("price", {})
                stock = p.get("stock", {})
                all_products.append({
                    "code": code,
                    "name": p.get("name", ""),
                    "price": price.get("formattedValue", "N/A") if isinstance(price, dict) else "N/A",
                    "stock": stock.get("stockLevelStatus", "unknown") if isinstance(stock, dict) else "unknown",
                    "image_url": image_url,
                    "summary": p.get("summary", ""),
                    "categories": [c.get("name", "") for c in p.get("categories", []) if isinstance(c, dict)],
                })

            total = data.get("pagination", {}).get("totalResults", 0)
            print(f"    Page {page + 1}: {len(products)} products (total={total})")
            if len(seen) >= total:
                break

    http.close()
    return all_products


def main():
    print("=== Product Image Indexer for CLIP Visual Search ===\n")

    products = fetch_products_with_images()
    print(f"\nTotal products with images: {len(products)}")

    if not products:
        print("No products with images found!")
        return

    from app.features.image_search import index_product_images
    indexed = index_product_images(products)
    print(f"\nDone! {indexed} product images indexed into Qdrant CLIP collection.")


if __name__ == "__main__":
    main()

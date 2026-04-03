"""
reset_and_load_qdrant.py
========================
deleting my old dB and uploading the new one
Run: python reset_and_load_qdrant.py
"""

import os
import re
import uuid
import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from fastembed import TextEmbedding

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SAP_URL        = os.getenv("SAP_BASE_URL", "https://ximena-excurved-sallowly.ngrok-free.dev/occ/v2")
SITE_ID        = os.getenv("SAP_SITE_ID", "electronics")
QDRANT_HOST     = os.getenv("QDRANT_HOST")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION     = "sap_products"   # agent's collection name

# ── Clients ───────────────────────────────────────────────────────────────────
qdrant   = QdrantClient(url=QDRANT_HOST, api_key=QDRANT_API_KEY)
embedder = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
http     = httpx.Client(timeout=30, verify=False)

_HTML_RE = re.compile(r"<[^>]+>")
def strip_html(text: str) -> str:
    """Remove HTML tags from SAP OCC search-highlight markup."""
    return _HTML_RE.sub("", text) if text else ""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Purane saare collections delete karo
# ─────────────────────────────────────────────────────────────────────────────
def nuke_all_collections():
    existing = [c.name for c in qdrant.get_collections().collections]
    if not existing:
        print("ℹ️  Koi collection nahi tha, skip.")
        return

    print(f"🗑️  {len(existing)} collection(s) delete ho rahe hain: {existing}")
    for name in existing:
        qdrant.delete_collection(name)
        print(f"  '{name}' deleted")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Naya collection banao
# ─────────────────────────────────────────────────────────────────────────────
def create_fresh_collection():
    qdrant.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )
    print(f" Naya collection '{COLLECTION}' ready")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: SAP se products fetch karo
# ─────────────────────────────────────────────────────────────────────────────
def fetch_products(query: str = "*", max_pages: int = 10) -> list[dict]:
    products, seen = [], set()

    for page in range(max_pages):
        resp = http.get(
            f"{SAP_URL}/{SITE_ID}/products/search",
            params={
                "query":       query,
                "pageSize":    20,
                "currentPage": page,
                "fields":      "FULL",
            },
        )
        if resp.status_code != 200:
            print(f"    Page {page} skip ({resp.status_code})")
            break

        data  = resp.json()
        batch = data.get("products", [])
        if not batch:
            break

        for p in batch:
            code = p.get("code")
            if code and code not in seen:
                products.append(p)
                seen.add(code)

        total = data.get("pagination", {}).get("totalResults", 0)
        print(f"   📄 Page {page+1}: {len(batch)} products (total available: {total})")

        if len(products) >= total:
            break

    return products


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3b: Enrich products with categories from detail API
# ─────────────────────────────────────────────────────────────────────────────
def enrich_with_categories(products: list[dict]) -> list[dict]:
    """Fetch categories from /products/{code} detail API (search API doesn't return them)."""
    total = len(products)
    for i, p in enumerate(products):
        code = p.get("code")
        if not code:
            continue
        try:
            resp = http.get(
                f"{SAP_URL}/{SITE_ID}/products/{code}",
                params={"fields": "categories(FULL)"},
            )
            if resp.status_code == 200:
                detail = resp.json()
                p["categories"] = detail.get("categories", [])
        except Exception as e:
            print(f"    ⚠ Could not fetch categories for {code}: {e}")

        if (i + 1) % 20 == 0 or (i + 1) == total:
            print(f"   📦 Enriched {i+1}/{total} products with categories")

    return products


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Embed + Qdrant mein daalo
# ─────────────────────────────────────────────────────────────────────────────
def embed_and_upsert(products: list[dict]):
    batch_size = 50
    uploaded   = 0

    for i in range(0, len(products), batch_size):
        batch = products[i : i + batch_size]

        # Embedding ke liye text banao
        texts = []
        for p in batch:
            cats = " ".join(strip_html(c.get("name", "")) for c in p.get("categories", []) if isinstance(c, dict))
            text = f"{strip_html(p.get('name',''))} {strip_html(p.get('summary',''))} {strip_html(p.get('description',''))} {cats}".strip()
            texts.append(text or p.get("code", "unknown"))

        vectors = [v.tolist() for v in embedder.embed(texts)]

        points = []
        base_media = SAP_URL.replace("/occ/v2", "")
        for product, vector in zip(batch, vectors):
            price = product.get("price") or {}
            stock = product.get("stock") or {}
            cats  = [c.get("name","") for c in product.get("categories",[]) if isinstance(c, dict)]

            # Extract best product image URL
            image_url = ""
            for fmt in ("zoom", "product", "thumbnail"):
                for img in product.get("images", []):
                    if img.get("imageType") == "PRIMARY" and img.get("format") == fmt:
                        url_path = img.get("url", "")
                        if url_path:
                            image_url = base_media + url_path if url_path.startswith("/") else url_path
                            break
                if image_url:
                    break

            points.append(PointStruct(
                id      = str(uuid.uuid4()),
                vector  = vector,
                payload = {
                    "code":        product.get("code", ""),
                    "name":        strip_html(product.get("name", "")),
                    "summary":     strip_html(product.get("summary", "")),
                    "description": strip_html((product.get("description") or "")[:500]),
                    "price":       price.get("formattedValue", "N/A") if isinstance(price, dict) else "N/A",
                    "price_value": price.get("value", 0) if isinstance(price, dict) else 0,
                    "stock":       stock.get("stockLevelStatus", "unknown") if isinstance(stock, dict) else "unknown",
                    "categories":  cats,
                    "rating":      product.get("averageRating"),
                    "image_url":   image_url,
                },
            ))

        qdrant.upsert(collection_name=COLLECTION, points=points)
        uploaded += len(points)
        print(f"     {uploaded}/{len(products)} uploaded")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*55)
    print("  Qdrant Reset + SAP Data Load")
    print("="*55)

    # Step 1
    print("\n[1/5] Purana data saaf kar raha hun...")
    nuke_all_collections()

    # Step 2
    print("\n[2/5] Naya collection bana raha hun...")
    create_fresh_collection()

    # Step 3: Multiple queries se zyada products milenge
    print("\n[3/5] SAP se products fetch kar raha hun...")
    QUERIES = [
        "camera", "digital camera", "DSLR", "compact camera",
        "lens", "zoom lens", "wide angle", "telephoto",
        "memory card", "SD card", "flash drive", "USB",
        "tripod", "camera bag", "camera case",
        "battery", "charger", "power bank",
        "flash", "lighting",
        "cable", "adapter", "accessory",
        "printer", "ink", "scanner",
        "phone", "tablet", "laptop",
        "headphones", "speaker", "tv",
    ]
    all_products, seen_codes = [], set()

    for q in QUERIES:
        print(f"\n   '{q}'")
        batch = fetch_products(query=q)
        for p in batch:
            if p.get("code") not in seen_codes:
                all_products.append(p)
                seen_codes.add(p["code"])

    print(f"\n   Total unique products: {len(all_products)}")

    # Step 3b: Enrich with categories
    print("\n[4/5] Categories fetch kar raha hun (detail API)...")
    enrich_with_categories(all_products)

    # Step 4
    print("\n[5/5] Embedding aur Qdrant mein daal raha hun...")
    embed_and_upsert(all_products)

    # Summary
    info = qdrant.get_collection(COLLECTION)
    print(f"\n{'='*55}")
    print(f"   Done!")
    print(f"  Collection : {COLLECTION}")
    print(f"  Points     : {info.points_count}")
    print(f"  Qdrant HOST : {QDRANT_HOST}")
    print(f"{'='*55}\n")
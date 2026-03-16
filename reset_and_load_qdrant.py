"""
reset_and_load_qdrant.py
========================
deleting my old dB and uploading the new one
Run: python reset_and_load_qdrant.py
"""

import os
import uuid
import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SAP_URL        = os.getenv("SAP_BASE_URL", "https://ximena-excurved-sallowly.ngrok-free.dev/occ/v2")
SITE_ID        = os.getenv("SAP_SITE_ID", "electronics")
QDRANT_HOST     = os.getenv("QDRANT_HOST")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION     = "sap_products"   # agent's collection name

# ── Clients ───────────────────────────────────────────────────────────────────
qdrant   = QdrantClient(url=QDRANT_HOST, api_key=QDRANT_API_KEY)
embedder = SentenceTransformer("all-MiniLM-L6-v2")
http     = httpx.Client(timeout=30, verify=False)


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
            cats = " ".join(c.get("name", "") for c in p.get("categories", []) if isinstance(c, dict))
            text = f"{p.get('name','')} {p.get('summary','')} {p.get('description','')} {cats}".strip()
            texts.append(text or p.get("code", "unknown"))

        vectors = embedder.encode(texts, show_progress_bar=False).tolist()

        points = []
        for product, vector in zip(batch, vectors):
            price = product.get("price") or {}
            stock = product.get("stock") or {}
            cats  = [c.get("name","") for c in product.get("categories",[]) if isinstance(c, dict)]

            points.append(PointStruct(
                id      = str(uuid.uuid4()),
                vector  = vector,
                payload = {
                    "code":        product.get("code", ""),
                    "name":        product.get("name", ""),
                    "summary":     product.get("summary", ""),
                    "description": (product.get("description") or "")[:500],
                    "price":       price.get("formattedValue", "N/A") if isinstance(price, dict) else "N/A",
                    "price_value": price.get("value", 0) if isinstance(price, dict) else 0,
                    "stock":       stock.get("stockLevelStatus", "unknown") if isinstance(stock, dict) else "unknown",
                    "categories":  cats,
                    "rating":      product.get("averageRating"),
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
    print("\n[1/4] Purana data saaf kar raha hun...")
    nuke_all_collections()

    # Step 2
    print("\n[2/4] Naya collection bana raha hun...")
    create_fresh_collection()

    # Step 3: Multiple queries se zyada products milenge
    print("\n[3/4] SAP se products fetch kar raha hun...")
    QUERIES = ["camera", "laptop", "phone", "headphone",
               "tv", "speaker", "tablet", "printer"]
    all_products, seen_codes = [], set()

    for q in QUERIES:
        print(f"\n   '{q}'")
        batch = fetch_products(query=q)
        for p in batch:
            if p.get("code") not in seen_codes:
                all_products.append(p)
                seen_codes.add(p["code"])

    print(f"\n   Total unique products: {len(all_products)}")

    # Step 4
    print("\n[4/4] Embedding aur Qdrant mein daal raha hun...")
    embed_and_upsert(all_products)

    # Summary
    info = qdrant.get_collection(COLLECTION)
    print(f"\n{'='*55}")
    print(f"   Done!")
    print(f"  Collection : {COLLECTION}")
    print(f"  Points     : {info.points_count}")
    print(f"  Qdrant HOST : {QDRANT_HOST}")
    print(f"{'='*55}\n")
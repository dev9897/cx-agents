"""
load_products_to_qdrant.py
==========================
SAP OCC API se products fetch karo aur Qdrant Cloud mein store karo.
Run: python load_products_to_qdrant.py
"""

import os
import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from fastembed import TextEmbedding
import uuid

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
SAP_URL        = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
SITE_ID        = os.getenv("SAP_SITE_ID", "electronics")
QDRANT_HOST     = os.getenv("QDRANT_HOST")          # Qdrant Cloud URL
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")      # Qdrant Cloud API Key
COLLECTION     = "sap_products"
EMBED_MODEL    = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim, fast & free

# ── Clients ───────────────────────────────────────────────────────────────────
qdrant = QdrantClient(url=QDRANT_HOST, api_key=QDRANT_API_KEY)
embedder = TextEmbedding(EMBED_MODEL)
http = httpx.Client(timeout=30, verify=False)  # verify=False for local SAP dev


def fetch_all_products(query: str = "*", max_pages: int = 10) -> list[dict]:
    """SAP OCC API se products fetch karo, pagination handle karo."""
    products = []
    page_size = 20

    for page in range(max_pages):
        url = f"{SAP_URL}/{SITE_ID}/products/search"
        params = {
            "query": query,
            "pageSize": page_size,
            "currentPage": page,
            "fields": "FULL",
        }
        resp = http.get(url, params=params)
        if resp.status_code != 200:
            print(f"  Page {page} failed: {resp.status_code}")
            break

        data = resp.json()
        batch = data.get("products", [])
        if not batch:
            break

        products.extend(batch)
        total = data.get("pagination", {}).get("totalResults", 0)
        print(f"   Page {page+1}: fetched {len(batch)} products (total={total})")

        if len(products) >= total:
            break

    return products


def create_collection_if_not_exists():
    """Qdrant mein collection banao agar exist nahi karti."""
    collections = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in collections:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(
                size=384,              # all-MiniLM-L6-v2 ka output dimension
                distance=Distance.COSINE,
            ),
        )
        print(f" Collection '{COLLECTION}' banayi")
    else:
        print(f" Collection '{COLLECTION}' pehle se exist karti hai")


def build_text_for_embedding(product: dict) -> str:
    """Product ka searchable text banao embedding ke liye."""
    parts = [
        product.get("name", ""),
        product.get("summary", ""),
        product.get("description", ""),
        " ".join(c.get("name", "") for c in product.get("categories", [])),
    ]
    return " ".join(p for p in parts if p).strip()


def load_products(queries: list[str] = None):
    """Main function: fetch → embed → upsert."""
    if queries is None:
        queries = ["camera", "laptop", "phone", "headphone", "tv"]

    print(f"\n SAP se products fetch kar raha hun...")
    all_products = []
    seen_codes = set()

    for q in queries:
        print(f"\n Query: '{q}'")
        products = fetch_all_products(query=q)
        for p in products:
            code = p.get("code")
            if code and code not in seen_codes:
                all_products.append(p)
                seen_codes.add(code)

    print(f"\n Total unique products: {len(all_products)}")
    if not all_products:
        print(" Koi product nahi mila!")
        return

    # Collection banao
    create_collection_if_not_exists()

    # Batch mein embed aur upsert karo
    batch_size = 50
    total_uploaded = 0

    for i in range(0, len(all_products), batch_size):
        batch = all_products[i:i + batch_size]

        # Text prepare karo
        texts = [build_text_for_embedding(p) for p in batch]

        # Embeddings banao
        print(f"     Batch {i//batch_size + 1}: {len(batch)} products embed kar raha hun...")
        vectors = [v.tolist() for v in embedder.embed(texts)]

        # Qdrant points banao
        points = []
        for product, vector in zip(batch, vectors):
            price_val = product.get("price", {})
            if isinstance(price_val, dict):
                price_formatted = price_val.get("formattedValue", "N/A")
                price_num = price_val.get("value", 0)
            else:
                price_formatted = "N/A"
                price_num = 0

            stock_info = product.get("stock", {})
            stock_status = stock_info.get("stockLevelStatus", "unknown") if isinstance(stock_info, dict) else "unknown"

            categories = [c.get("name", "") for c in product.get("categories", []) if isinstance(c, dict)]

            points.append(PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "code":        product.get("code", ""),
                    "name":        product.get("name", ""),
                    "summary":     product.get("summary", ""),
                    "description": product.get("description", "")[:500],  # truncate
                    "price":       price_formatted,
                    "price_value": price_num,
                    "stock":       stock_status,
                    "categories":  categories,
                    "rating":      product.get("averageRating"),
                },
            ))

        # Qdrant upsert
        qdrant.upsert(collection_name=COLLECTION, points=points)
        total_uploaded += len(points)
        print(f"    {total_uploaded}/{len(all_products)} products upload ho gaye")

    print(f"\n Done! {total_uploaded} products Qdrant mein load ho gaye")
    print(f"   Collection: {COLLECTION}")
    print(f"   Qdrant HOST: {QDRANT_HOST}")


if __name__ == "__main__":
    print(" SAP URL:", f"{SAP_URL}/{SITE_ID}/products/search")
    load_products(queries=["camera", "laptop", "phone", "headphone", "tv", "speaker"])
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import TypedDict

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PRODUCT_COLLECTION  = "product_catalog"
VECTOR_SIZE         = 384
SEARCH_LIMIT        = 20
SCORE_THRESHOLD     = 0.25  

_PRODUCT_NS = uuid.UUID("b2c3d4e5-f6a7-8901-bcde-f12345678901")


# ─────────────────────────────────────────────────────────────────────────────
# TypedDicts
# ─────────────────────────────────────────────────────────────────────────────

class ProductRecord(TypedDict, total=False):
    product_code: str    # SAP product code — used as the primary key
    name:         str    # display name, e.g. "Michelin Pilot Sport 4 205/55 R16"
    category:     str    # e.g. "tyres", "wheels", "cameras"
    description:  str    # short freetext description from SAP catalog
    price_range:  str    # "low" | "medium" | "high"


class CBFRecommendation(TypedDict):
    item:      str    # product_code
    name:      str    # display name
    category:  str
    cbf_score: float  # cosine similarity score


# ─────────────────────────────────────────────────────────────────────────────
# Lazy singletons (same pattern as user_memory.py)
# ─────────────────────────────────────────────────────────────────────────────

_qdrant_client    = None
_embedder_model   = None
_collection_ready = False


def _get_client():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        host    = os.getenv("QDRANT_HOST")
        api_key = os.getenv("QDRANT_API_KEY")
        if not host:
            raise RuntimeError(
                "QDRANT_HOST is not set. Add it to your .env file."
            )
        _qdrant_client = QdrantClient(url=host, api_key=api_key)
        logger.debug("CBF QdrantClient initialised (host=%s)", host)
    return _qdrant_client


def _get_embedder():
    global _embedder_model
    if _embedder_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("CBF: loading SentenceTransformer model...")
        _embedder_model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("CBF: SentenceTransformer model ready.")
    return _embedder_model


# ─────────────────────────────────────────────────────────────────────────────
# Collection bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def ensure_product_collection() -> None:
    global _collection_ready
    if _collection_ready:
        return

    from qdrant_client.models import Distance, VectorParams

    client   = _get_client()
    existing = {c.name for c in client.get_collections().collections}

    if PRODUCT_COLLECTION not in existing:
        client.create_collection(
            collection_name=PRODUCT_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '%s'", PRODUCT_COLLECTION)
    else:
        logger.debug("Qdrant collection '%s' already exists", PRODUCT_COLLECTION)

    _collection_ready = True


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _product_id(product: ProductRecord) -> str:
    key    = product.get("product_code", "") or product.get("name", "")
    digest = hashlib.md5(key.encode()).hexdigest()
    return str(uuid.uuid5(_PRODUCT_NS, digest))


def _product_text(product: ProductRecord) -> str:
    parts = [
        product.get("name", ""),
        product.get("category", ""),
        product.get("description", ""),
    ]
    return " ".join(p for p in parts if p).strip() or "unknown product"


def _embed(text: str) -> list[float]:
    return _get_embedder().encode(text, normalize_embeddings=True).tolist()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def index_products(products: list[ProductRecord], batch_size: int = 50) -> None:
    try:
        ensure_product_collection()
        from qdrant_client.models import PointStruct

        points = []
        for product in products:
            point_id = _product_id(product)
            vector   = _embed(_product_text(product))
            points.append(PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "product_code": product.get("product_code", ""),
                    "name":         product.get("name", ""),
                    "category":     product.get("category", ""),
                    "description":  product.get("description", ""),
                    "price_range":  product.get("price_range", ""),
                },
            ))

        client = _get_client()
        for i in range(0, len(points), batch_size):
            batch = points[i:i + batch_size]
            client.upsert(collection_name=PRODUCT_COLLECTION, points=batch)
            logger.debug("CBF indexed batch %d-%d", i, i + len(batch))

        logger.info("CBF index_products complete — %d products indexed", len(products))

    except Exception as exc:
        logger.error("CBF index_products FAILED: %s: %s", type(exc).__name__, exc)


def get_cbf_recommendations(
    current_query: str,
    top_k: int = 5,
    exclude_product_code: str = "",
) -> list[CBFRecommendation]:
   
    try:
        ensure_product_collection()

        query_vec = _embed(current_query)
     
        # NEW — fix for the query error 
        results = _get_client().query_points(
            collection_name=PRODUCT_COLLECTION,
            query=query_vec,
            limit=SEARCH_LIMIT,
            score_threshold=SCORE_THRESHOLD,
        ).points

        recs: list[CBFRecommendation] = []
        for hit in results:
            code = hit.payload.get("product_code", "")
            if code and code == exclude_product_code:
                continue

            recs.append(CBFRecommendation(
                item      = code or hit.payload.get("category", ""),
                name      = hit.payload.get("name", ""),
                category  = hit.payload.get("category", ""),
                cbf_score = round(hit.score, 3),
            ))

            if len(recs) >= top_k:
                break

        logger.debug(
            "CBF get_cbf_recommendations | query='%s' -> %d recs",
            current_query[:60], len(recs),
        )
        return recs

    except Exception as exc:
        logger.warning(
            "CBF get_cbf_recommendations FAILED: %s: %s", type(exc).__name__, exc
        )
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Sample product catalog
# Replace / extend with your real SAP catalog data.
# Run:  python content_memory.py --index
# ─────────────────────────────────────────────────────────────────────────────

# SAMPLE_PRODUCTS: list[ProductRecord] = [
#     # Tyres
#     {"product_code": "TYRE-4X4",  "name": "Mud-terrain 4x4 tyre 265/70 R17",         "category": "tyres",      "description": "Heavy duty off-road mud terrain tyre for 4WD and SUV",              "price_range": "high"},
#     {"product_code": "TYRE-AT",   "name": "All-terrain tyre 265/65 R17",              "category": "tyres",      "description": "All terrain tyre suitable for highway and light off-road use",      "price_range": "high"},
#     {"product_code": "TYRE-SUM",  "name": "Summer performance tyre 205/55 R16",       "category": "tyres",      "description": "High performance summer tyre for passenger cars",                   "price_range": "medium"},
#     {"product_code": "TYRE-WIN",  "name": "Winter tyre 205/55 R16",                   "category": "tyres",      "description": "Winter tyre with superior grip on snow and ice",                   "price_range": "medium"},
#     {"product_code": "TYRE-MUD",  "name": "Mud tyre extreme off-road 285/75 R16",     "category": "tyres",      "description": "Extreme mud tyre for serious off-road and rock crawling",           "price_range": "high"},

#     # Wheels
#     {"product_code": "WHEEL-ST",  "name": "Steel off-road wheel 17 inch",             "category": "wheels",     "description": "Heavy duty steel wheel for 4x4 and off-road vehicles",             "price_range": "medium"},
#     {"product_code": "WHEEL-AL",  "name": "Alloy wheel 16 inch 5-stud",               "category": "wheels",     "description": "Lightweight alloy wheel for passenger and sport vehicles",          "price_range": "medium"},
#     {"product_code": "WHEEL-SP",  "name": "Sport alloy wheel 18 inch",                "category": "wheels",     "description": "High performance alloy wheel for sports cars and hot hatches",      "price_range": "high"},

#     # Accessories
#     {"product_code": "MUD-HD",    "name": "Heavy duty mud flaps universal fit",        "category": "accessories","description": "Robust mud flaps suitable for 4x4, SUV and trucks",                 "price_range": "low"},
#     {"product_code": "TRIP-CF",   "name": "Carbon fibre camera tripod lightweight",    "category": "accessories","description": "Ultra-light carbon fibre tripod for photographers and videographers","price_range": "medium"},
#     {"product_code": "FILT-UV",   "name": "UV lens filter 52mm",                       "category": "accessories","description": "UV protection filter compatible with most 52mm camera lenses",      "price_range": "low"},
#     {"product_code": "FILT-ND",   "name": "ND filter set 3 6 10 stop",                "category": "accessories","description": "Neutral density filter set for long exposure and video photography", "price_range": "medium"},

#     # Cameras and lenses
#     {"product_code": "LENS-200",  "name": "Telephoto lens 70-200mm f2.8",             "category": "lenses",     "description": "Professional telephoto zoom lens for sports and wildlife photography","price_range": "high"},
#     {"product_code": "LENS-50",   "name": "Standard prime lens 50mm f1.8",            "category": "lenses",     "description": "Versatile 50mm prime lens ideal for portraits and street photography","price_range": "medium"},
#     {"product_code": "LENS-WA",   "name": "Wide angle lens 16-35mm f4",               "category": "lenses",     "description": "Wide angle zoom lens for landscapes and architectural photography",   "price_range": "high"},
#     {"product_code": "CAM-MLS",   "name": "Mirrorless camera body 24MP",              "category": "cameras",    "description": "Full-frame mirrorless camera body with 24 megapixel sensor",         "price_range": "high"},

#     # Engine oil and filters
#     {"product_code": "OIL-5W4",   "name": "Synthetic engine oil 5W-40 5 litre",       "category": "engine_oil", "description": "Fully synthetic engine oil for petrol and diesel engines",          "price_range": "medium"},
#     {"product_code": "OIL-FS",    "name": "Full synthetic motor oil 0W-20 5 litre",   "category": "engine_oil", "description": "Ultra-low viscosity full synthetic oil for modern fuel efficient engines","price_range": "high"},
#     {"product_code": "FILT-OIL",  "name": "Engine oil filter for 2.0L diesel",        "category": "filters",    "description": "OEM-compatible oil filter for 2.0 litre diesel engines",            "price_range": "low"},
#     {"product_code": "FILT-AIR",  "name": "Engine air filter panel type",             "category": "filters",    "description": "High flow replacement air filter for improved engine performance",   "price_range": "low"},
#     {"product_code": "FILT-CAB",  "name": "Cabin pollen air filter",                  "category": "filters",    "description": "Cabin air filter to remove pollen, dust and allergens",             "price_range": "low"},

#     # Engine parts
#     {"product_code": "SPARK-IR",  "name": "Iridium spark plugs set of 4",             "category": "engine",     "description": "Long-life iridium spark plugs for improved ignition and fuel economy","price_range": "medium"},
#     {"product_code": "COOL-AF",   "name": "Engine coolant antifreeze 5 litre",        "category": "engine",     "description": "Long-life antifreeze coolant concentrate for all engine types",     "price_range": "medium"},

#     # Brakes
#     {"product_code": "BRAKE-F",   "name": "Front brake pads ceramic performance",     "category": "brakes",     "description": "Low dust ceramic front brake pads for passenger and sport vehicles","price_range": "medium"},
#     {"product_code": "BRAKE-D",   "name": "Front brake disc rotor pair",              "category": "brakes",     "description": "Replacement front brake discs compatible with most passenger cars",  "price_range": "medium"},
#     {"product_code": "BRK-FLD",   "name": "Brake fluid DOT4 500ml",                  "category": "service",    "description": "DOT4 synthetic brake fluid for disc and drum braking systems",       "price_range": "low"},

#     # Alignment
#     {"product_code": "ALIGN-KIT", "name": "Wheel alignment adjustment kit",           "category": "alignment",  "description": "Camber and toe adjustment kit for performance wheel alignment",     "price_range": "medium"},
# ]


# if __name__ == "__main__":
#     import argparse
#     parser = argparse.ArgumentParser(description="CBF product catalog management")
#     parser.add_argument("--index",   action="store_true", help="Index sample products into Qdrant")
#     parser.add_argument("--search",  type=str, default="", help="Test a search query")
#     parser.add_argument("--top-k",   type=int, default=5,  help="Number of results (default: 5)")
#     args = parser.parse_args()

#     logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

#     if args.index:
#         print(f"Indexing {len(SAMPLE_PRODUCTS)} sample products into '{PRODUCT_COLLECTION}'...")
#         index_products(SAMPLE_PRODUCTS)
#         print("Done.")

#     if args.search:
#         print(f"\nCBF search: '{args.search}'")
#         recs = get_cbf_recommendations(args.search, top_k=args.top_k)
#         if recs:
#             for r in recs:
#                 print(f"  [{r['cbf_score']:.3f}] {r['item']:<12} {r['name']}")
#         else:
#             print("  No results — is the product catalog indexed? Run: python content_memory.py --index")
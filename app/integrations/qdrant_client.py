"""Qdrant vector search for semantic product discovery."""

import logging
import os

from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()

logger = logging.getLogger("sap_agent.qdrant")

QDRANT_URL = os.getenv("QDRANT_HOST")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = "sap_products"

_qdrant = None
_embedder = None


def _get_client():
    global _qdrant
    if _qdrant is None:
        if not QDRANT_URL:
            raise RuntimeError("QDRANT_HOST is not set in .env")
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(
            url=QDRANT_URL, api_key=QDRANT_API_KEY,
            timeout=30, check_compatibility=False,
        )
        logger.info("Qdrant client initialised: %s", QDRANT_URL)
    return _qdrant


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
        logger.info("FastEmbed loaded (all-MiniLM-L6-v2)")
    return _embedder


def is_qdrant_configured() -> bool:
    return bool(QDRANT_URL)


@tool
def semantic_search_products(query: str, top_k: int = 5) -> dict:
    """
    Semantic product search using the Qdrant vector database.
    Use this for natural language or intent-based queries such as:
    'camera for outdoor travel', 'budget digital camera'.
    Returns product code, name, price, stock status, and relevance score.
    """
    try:
        client = _get_client()
        embedder = _get_embedder()
        vector = list(embedder.embed([query]))[0].tolist()
        response = client.query_points(
            collection_name=COLLECTION_NAME, query=vector,
            limit=top_k, score_threshold=0.35, with_payload=True,
        )
        results = response.points
        if not results:
            return {"success": True, "products": [],
                    "message": "No semantically relevant products found."}
        return {
            "success": True,
            "total": len(results),
            "products": [
                {
                    "code": hit.payload.get("code"),
                    "name": hit.payload.get("name"),
                    "price": hit.payload.get("price"),
                    "stock": hit.payload.get("stock"),
                    "summary": hit.payload.get("summary", "")[:150],
                    "score": round(hit.score, 3),
                }
                for hit in results
            ],
        }
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("semantic_search_products failed")
        return {"success": False, "error": f"Qdrant search error: {e}"}

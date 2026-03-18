"""
qdrant_tool.py
==============
Qdrant semantic product search tool — for the SAP agent.
"""

import logging
import os
from typing import Optional

from langchain_core.tools import tool
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sap_agent.qdrant")

QDRANT_URL      = os.getenv("QDRANT_HOST")
QDRANT_API_KEY  = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = "sap_products"

# ── Lazy singletons — only initialised on first tool call ─────────────────────
_qdrant   = None
_embedder = None


def _get_client():
    global _qdrant
    if _qdrant is None:
        if not QDRANT_URL:
            raise RuntimeError("QDRANT_HOST is not set in .env")
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(
            url=QDRANT_URL,
            api_key=QDRANT_API_KEY,  # None is fine for local Qdrant (no auth)
            timeout=30,
            check_compatibility=False,
        )
        logger.info("Qdrant client initialised → %s", QDRANT_URL)
    return _qdrant


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
        logger.info("FastEmbed loaded (all-MiniLM-L6-v2)")
    return _embedder


def is_qdrant_configured() -> bool:
    """Returns True if QDRANT_HOST is set — used by production_agent to decide
    whether to include this tool in the agent's tool list."""
    return bool(QDRANT_URL)


@tool
def semantic_search_products(query: str, top_k: int = 5) -> dict:
    """
    Semantic product search using the Qdrant vector database.
    Use this for natural language or intent-based queries such as:
    'camera for outdoor travel', 'memory card for 4K video', 'budget digital camera'.
    Returns product code, name, price, stock status, and a relevance score.
    Prefer this over SAP keyword search when the user describes what they need
    rather than naming a specific product.
    """
    try:
        client   = _get_client()
        embedder = _get_embedder()

        vector = list(embedder.embed([query]))[0].tolist()

        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=top_k,
            score_threshold=0.35,
            with_payload=True,
        )

        results = response.points

        if not results:
            return {
                "success": True,
                "products": [],
                "message": "No semantically relevant products found for that query.",
            }

        return {
            "success":  True,
            "total":    len(results),
            "products": [
                {
                    "code":    hit.payload.get("code"),
                    "name":    hit.payload.get("name"),
                    "price":   hit.payload.get("price"),
                    "stock":   hit.payload.get("stock"),
                    "summary": hit.payload.get("summary", "")[:150],
                    "score":   round(hit.score, 3),
                }
                for hit in results
            ],
        }

    except RuntimeError as e:
        logger.error("Qdrant not configured: %s", e)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("semantic_search_products failed")
        return {"success": False, "error": f"Qdrant search error: {e}"}

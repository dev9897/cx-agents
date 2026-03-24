from __future__ import annotations

import hashlib
import logging
import os
import uuid
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

USER_COLLECTION    = "user_profiles"
VECTOR_SIZE        = 384          
SEARCH_LIMIT       = 100          
SCORE_THRESHOLD    = 0.4         
MAX_SCORE_PER_USER = 2.0          

# Action weights: stronger intent = higher weight (#6)
ACTION_WEIGHTS: dict[str, float] = {
    "order":       3.0,
    "add_to_cart": 2.0,
    "search":      1.0,
}

# Deterministic UUID namespace for idempotent upserts (#1)
_INTERACTION_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


# ─────────────────────────────────────────────────────────────────────────────
# TypedDict for interaction payloads (#8)
# ─────────────────────────────────────────────────────────────────────────────

from typing import TypedDict


class Interaction(TypedDict, total=False):
    action:       str  
    query:        str   
    product_code: str   
    category:     str   
    price_range:  str   


# ─────────────────────────────────────────────────────────────────────────────
# Lazy singletons (#4, #5)
# ─────────────────────────────────────────────────────────────────────────────

_qdrant_client  = None
_embedder_model = None
_collection_ready = False   


def _get_client():
    """Return (and lazily create) the shared QdrantClient."""
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        host    = os.getenv("QDRANT_HOST")
        api_key = os.getenv("QDRANT_API_KEY")
        if not host:
            raise RuntimeError(
                "QDRANT_HOST is not set. "
                "Add it to your .env file before using user_memory."
            )
        _qdrant_client = QdrantClient(url=host, api_key=api_key)
        logger.debug("QdrantClient initialised (host=%s)", host)
    return _qdrant_client


def _get_embedder():
    """Return (and lazily load) the shared SentenceTransformer model."""
    global _embedder_model
    if _embedder_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading SentenceTransformer model — this may take a moment…")
        _embedder_model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("SentenceTransformer model loaded.")
    return _embedder_model


# ─────────────────────────────────────────────────────────────────────────────
# Collection bootstrap (#2 — called once, result cached)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_user_collection() -> None:
    global _collection_ready
    if _collection_ready:
        return

    from qdrant_client.models import Distance, VectorParams

    client   = _get_client()
    existing = {c.name for c in client.get_collections().collections}

    if USER_COLLECTION not in existing:
        client.create_collection(
            collection_name=USER_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        logger.info("Created Qdrant collection '%s'", USER_COLLECTION)
    else:
        logger.debug("Qdrant collection '%s' already exists", USER_COLLECTION)

    _collection_ready = True


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _interaction_id(username: str, interaction: Interaction) -> str:
    key = "|".join([
        username,
        interaction.get("action", ""),
        interaction.get("product_code", ""),
        interaction.get("query", ""),
    ])
    digest = hashlib.md5(key.encode()).hexdigest()
    return str(uuid.uuid5(_INTERACTION_NS, digest))


def _embed(text: str) -> list[float]:
    return _get_embedder().encode(text, normalize_embeddings=True).tolist()


def _semantic_text(interaction: Interaction) -> str:
    
    parts = [
        interaction.get("query", ""),
        interaction.get("category", ""),
    ]
    return " ".join(p for p in parts if p).strip() or "general"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def save_user_interaction(username: str, interaction: Interaction) -> None:
    
    try:
        ensure_user_collection()

        from qdrant_client.models import PointStruct

        point_id = _interaction_id(username, interaction)
        vector   = _embed(_semantic_text(interaction))
        weight   = ACTION_WEIGHTS.get(interaction.get("action", "search"), 1.0)

        _get_client().upsert(
            collection_name=USER_COLLECTION,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "username":     username,
                        "action":       interaction.get("action", ""),
                        "query":        interaction.get("query", ""),
                        "product_code": interaction.get("product_code", ""),
                        "category":     interaction.get("category", ""),
                        "price_range":  interaction.get("price_range", ""),
                        "weight":       weight,   
                    },
                )
            ],
        )
        logger.debug(
            "save_user_interaction | user=%s action=%s product=%s category=%s",
            username,
            interaction.get("action"),
            interaction.get("product_code", "-"),
            interaction.get("category", "-"),
        )

    except Exception as exc:
        logger.warning(
            "save_user_interaction | FAILED for user=%s | %s: %s",
            username, type(exc).__name__, exc,
        )


def get_cf_recommendations(
    username: str,
    current_query: str,
    top_k: int = 6,
) -> list[dict]:
   
    try:
        ensure_user_collection()

        query_vec = _embed(current_query)

      

        # NEW — fix for the query failed problem
        from qdrant_client.models import QueryRequest
        results = _get_client().query_points(
          collection_name=USER_COLLECTION,
          query=query_vec,
          limit=SEARCH_LIMIT,
          score_threshold=SCORE_THRESHOLD,
        ).points


        item_user_scores: dict[str, dict[str, float]] = {}

        for hit in results:
            hit_user = hit.payload.get("username", "")
            if hit_user == username:
                continue   

            product_code = hit.payload.get("product_code", "")
            category     = hit.payload.get("category", "")
            item_key     = product_code or category
            # item_key = product_code  
            if not item_key:
                continue

            weight        = float(hit.payload.get("weight", 1.0))
            weighted_score = hit.score * weight

            user_scores = item_user_scores.setdefault(item_key, {})
            if weighted_score > user_scores.get(hit_user, 0.0):
                user_scores[hit_user] = weighted_score

        aggregated: dict[str, float] = {}
        for item_key, user_scores in item_user_scores.items():
            total = sum(
                min(score, MAX_SCORE_PER_USER)
                for score in user_scores.values()
            )
            aggregated[item_key] = total

        ranked = sorted(aggregated.items(), key=lambda x: x[1], reverse=True)

        recommendations = [
            {"item": key, "cf_score": round(score, 3)}
            for key, score in ranked[:top_k]
        ]

        logger.debug(
            "get_cf_recommendations | user=%s query='%s' -> %d recs",
            username, current_query[:60], len(recommendations),
        )
        return recommendations

    except Exception as exc:
        logger.warning(
            "get_cf_recommendations | FAILED for user=%s | %s: %s",
            username, type(exc).__name__, exc,
        )
        return []
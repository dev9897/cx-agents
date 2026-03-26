"""
Recommendation Engine — Collaborative + Content-Based Filtering via Qdrant.

Architecture:
  1. On user login → fetch their order history + browsing data from SAP
  2. Build a user preference vector from purchased/browsed product embeddings
  3. Collaborative filtering: find similar users via Qdrant, recommend their products
  4. Content filtering: find products similar to user's purchase history
  5. Blend both signals with configurable weights

Qdrant collections:
  - sap_products          : product catalog vectors (existing)
  - user_profiles         : user preference vectors (new)
  - user_interactions     : per-user product interaction log (new)
"""

import logging
import os
import uuid
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from langchain_core.tools import tool, BaseTool
from pydantic import BaseModel

from app.features.registry import BaseFeature

load_dotenv()

logger = logging.getLogger("sap_agent.features.recommendations")

# ── Config ───────────────────────────────────────────────────────────────────

QDRANT_URL = os.getenv("QDRANT_HOST")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
PRODUCTS_COLLECTION = "sap_products"
USER_PROFILES_COLLECTION = "user_profiles"
COLLABORATIVE_WEIGHT = float(os.getenv("RECO_COLLABORATIVE_WEIGHT", "0.4"))
CONTENT_WEIGHT = float(os.getenv("RECO_CONTENT_WEIGHT", "0.6"))
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2

# ── Lazy-loaded clients ──────────────────────────────────────────────────────

_qdrant = None
_embedder = None


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY,
                               timeout=30, check_compatibility=False)
    return _qdrant


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return _embedder


# ── Qdrant helpers ───────────────────────────────────────────────────────────

def _ensure_user_profiles_collection():
    """Create user_profiles collection if it doesn't exist."""
    client = _get_qdrant()
    from qdrant_client.models import Distance, VectorParams
    collections = [c.name for c in client.get_collections().collections]
    if USER_PROFILES_COLLECTION not in collections:
        client.create_collection(
            collection_name=USER_PROFILES_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        logger.info("Created collection: %s", USER_PROFILES_COLLECTION)


def _build_user_preference_vector(product_texts: list[str]) -> list[float]:
    """Average product embeddings to create a user preference vector."""
    if not product_texts:
        return [0.0] * EMBEDDING_DIM
    embedder = _get_embedder()
    vectors = list(embedder.embed(product_texts))
    avg = np.mean([v for v in vectors], axis=0)
    return (avg / (np.linalg.norm(avg) + 1e-8)).tolist()


def _upsert_user_profile(user_id: str, preference_vector: list[float],
                         purchased_codes: list[str]):
    """Store/update the user preference vector in Qdrant."""
    client = _get_qdrant()
    from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
    _ensure_user_profiles_collection()

    # Check if user already exists
    existing = client.scroll(
        collection_name=USER_PROFILES_COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="user_id", match=MatchValue(value=user_id))
        ]),
        limit=1,
    )
    point_id = existing[0][0].id if existing[0] else str(uuid.uuid4())

    client.upsert(
        collection_name=USER_PROFILES_COLLECTION,
        points=[PointStruct(
            id=point_id,
            vector=preference_vector,
            payload={
                "user_id": user_id,
                "purchased_codes": purchased_codes,
            },
        )],
    )


# ── Core Recommendation Logic ────────────────────────────────────────────────

def get_content_recommendations(user_id: str, purchased_codes: list[str],
                                preference_vector: list[float],
                                top_k: int = 10) -> list[dict]:
    """Content-based: find products similar to user's purchase history."""
    client = _get_qdrant()

    results = client.query_points(
        collection_name=PRODUCTS_COLLECTION,
        query=preference_vector,
        limit=top_k + len(purchased_codes),
        score_threshold=0.3,
        with_payload=True,
    )

    recommendations = []
    for hit in results.points:
        code = hit.payload.get("code", "")
        if code not in purchased_codes:
            recommendations.append({
                "code": code,
                "name": hit.payload.get("name", ""),
                "price": hit.payload.get("price", ""),
                "stock": hit.payload.get("stock", ""),
                "summary": hit.payload.get("summary", "")[:150],
                "image_url": hit.payload.get("image_url", ""),
                "score": round(hit.score, 3),
                "reason": "content",
            })
        if len(recommendations) >= top_k:
            break
    return recommendations


def get_collaborative_recommendations(user_id: str,
                                      preference_vector: list[float],
                                      purchased_codes: list[str],
                                      top_k: int = 10) -> list[dict]:
    """Collaborative: find similar users and recommend their products."""
    client = _get_qdrant()

    # Find similar users
    similar_users = client.query_points(
        collection_name=USER_PROFILES_COLLECTION,
        query=preference_vector,
        limit=6,
        score_threshold=0.3,
        with_payload=True,
    )

    # Collect products from similar users that this user hasn't bought
    candidate_codes = set()
    for hit in similar_users.points:
        if hit.payload.get("user_id") != user_id:
            for code in hit.payload.get("purchased_codes", []):
                if code not in purchased_codes:
                    candidate_codes.add(code)

    if not candidate_codes:
        return []

    # Fetch product details from Qdrant for candidate codes
    from qdrant_client.models import Filter, FieldCondition, MatchAny
    results = client.scroll(
        collection_name=PRODUCTS_COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="code", match=MatchAny(any=list(candidate_codes)))
        ]),
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )

    recommendations = []
    for point in results[0]:
        recommendations.append({
            "code": point.payload.get("code", ""),
            "name": point.payload.get("name", ""),
            "price": point.payload.get("price", ""),
            "stock": point.payload.get("stock", ""),
            "summary": point.payload.get("summary", "")[:150],
            "image_url": point.payload.get("image_url", ""),
            "score": 0.8,  # collaborative score placeholder
            "reason": "collaborative",
        })
    return recommendations[:top_k]


def get_blended_recommendations(user_id: str, purchased_product_texts: list[str],
                                purchased_codes: list[str],
                                top_k: int = 8) -> dict:
    """Blend collaborative + content-based recommendations."""
    if not purchased_product_texts:
        return {
            "success": True,
            "recommendations": [],
            "message": "No purchase history available for recommendations.",
        }

    preference_vector = _build_user_preference_vector(purchased_product_texts)
    _upsert_user_profile(user_id, preference_vector, purchased_codes)

    content_recs = get_content_recommendations(
        user_id, purchased_codes, preference_vector, top_k=top_k)
    collab_recs = get_collaborative_recommendations(
        user_id, preference_vector, purchased_codes, top_k=top_k)

    # Blend: merge and deduplicate, weighted scoring
    seen = set()
    blended = []

    for rec in content_recs:
        code = rec["code"]
        if code not in seen:
            rec["blended_score"] = rec["score"] * CONTENT_WEIGHT
            blended.append(rec)
            seen.add(code)

    for rec in collab_recs:
        code = rec["code"]
        if code in seen:
            # Boost score if both signals agree
            for b in blended:
                if b["code"] == code:
                    b["blended_score"] += rec["score"] * COLLABORATIVE_WEIGHT
                    b["reason"] = "both"
                    break
        else:
            rec["blended_score"] = rec["score"] * COLLABORATIVE_WEIGHT
            blended.append(rec)
            seen.add(code)

    blended.sort(key=lambda x: x["blended_score"], reverse=True)
    return {
        "success": True,
        "total": len(blended[:top_k]),
        "recommendations": blended[:top_k],
    }


# ── Agent Tool ───────────────────────────────────────────────────────────────

@tool
def get_personalized_recommendations(user_email: str = "",
                                     access_token: str = "") -> dict:
    """
    Get personalized product recommendations for the logged-in user.
    Uses their order history to find products they might like.
    Combines collaborative filtering (what similar users bought) and
    content-based filtering (products similar to past purchases).
    Call this when a user logs in or asks for recommendations.
    """
    if not user_email:
        return {"success": False,
                "error": "User must be logged in for personalized recommendations."}

    try:
        from app.integrations import sap_client
        # Fetch user's order history to build preference profile
        orders_data = sap_client.get_user_orders(access_token, page_size=20)
        if not orders_data.get("success"):
            return {"success": True, "recommendations": [],
                    "message": "Could not fetch order history. Try browsing our catalog!"}

        purchased_codes = []
        purchased_texts = []
        for order in orders_data.get("orders", []):
            for entry in order.get("entries", []):
                code = entry.get("product_code", "")
                name = entry.get("product_name", "")
                if code and code not in purchased_codes:
                    purchased_codes.append(code)
                    purchased_texts.append(name)

        if not purchased_codes:
            return {"success": True, "recommendations": [],
                    "message": "No purchase history yet. Browse our catalog to get started!"}

        result = get_blended_recommendations(
            user_id=user_email,
            purchased_product_texts=purchased_texts,
            purchased_codes=purchased_codes,
        )
        return result

    except Exception as e:
        logger.exception("get_personalized_recommendations failed")
        return {"success": False, "error": f"Recommendation error: {e}"}


# ── API Routes ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])

_sessions: dict = {}


def set_session_store(sessions: dict):
    global _sessions
    _sessions = sessions


class RecommendationResponse(BaseModel):
    success: bool
    recommendations: list[dict] = []
    message: str = ""


@router.get("", response_model=RecommendationResponse)
def get_recommendations(session_id: str = Query(...)):
    """Get recommendations for the current user session."""
    state = _sessions.get(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")

    access_token = state.get("access_token", "")
    user_email = state.get("user_email", "")

    if not access_token or not user_email:
        return RecommendationResponse(
            success=True, recommendations=[],
            message="Please sign in to get personalized recommendations.")

    try:
        result = get_personalized_recommendations.invoke({
            "user_email": user_email,
            "access_token": access_token,
        })
        return RecommendationResponse(**result)
    except Exception as e:
        logger.exception("Recommendation API failed")
        return RecommendationResponse(
            success=False, message=f"Error: {e}")


# ── Feature Registration ─────────────────────────────────────────────────────

class RecommendationFeature(BaseFeature):
    @property
    def name(self) -> str:
        return "recommendations"

    @property
    def description(self) -> str:
        return "AI-powered product recommendations (collaborative + content filtering)"

    def is_available(self) -> bool:
        return bool(QDRANT_URL)

    def get_tools(self) -> list[BaseTool]:
        return [get_personalized_recommendations]

    def get_router(self) -> Optional[APIRouter]:
        return router

    def get_ui_config(self) -> dict:
        return {
            "enabled": True,
            "name": self.name,
            "show_on_login": True,
            "carousel": True,
        }

from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Blending constants  (tune these for your dataset)
# ─────────────────────────────────────────────────────────────────────────────

CF_FULL_WEIGHT_AT = 10    
CF_MAX_WEIGHT     = 0.80  


# for fileration
_FILLER_WORDS = frozenset({
    "hey", "agent", "can", "you", "show", "me", "some", "please",
    "find", "looking", "for", "want", "need", "get", "ones", "these",
    "are", "too", "cheap", "expensive", "good", "nice", "best",
    "i", "a", "the", "is", "it", "my", "have", "do",
})

def _clean_query(text: str) -> str:
    """Strip filler words, keep product-relevant terms."""
    words = [w for w in text.lower().split() if w not in _FILLER_WORDS]
    return " ".join(words) if words else text


class HybridRecommendation(TypedDict):
    item:         str
    name:         str
    category:     str
    cf_score:     float
    cbf_score:    float
    hybrid_score: float
    source:       str  


# ─────────────────────────────────────────────────────────────────────────────
# Interaction counter
# ─────────────────────────────────────────────────────────────────────────────

def _count_user_interactions(username: str) -> int:

    try:
        from memory_history.user_memory import _get_client, USER_COLLECTION, ensure_user_collection
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        ensure_user_collection()
        result = _get_client().count(
            collection_name=USER_COLLECTION,
            count_filter=Filter(
                must=[FieldCondition(key="username", match=MatchValue(value=username))]
            ),
            exact=False,   # approximate is fine and faster
        )
        return result.count

    except Exception as exc:
        logger.debug("_count_user_interactions failed for user=%s: %s", username, exc)
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Weight calculation
# ─────────────────────────────────────────────────────────────────────────────

def _blend_weights(interaction_count: int) -> tuple[float, float]:
   
    cf_weight  = min(interaction_count / CF_FULL_WEIGHT_AT, CF_MAX_WEIGHT)
    cbf_weight = round(1.0 - cf_weight, 4)
    cf_weight  = round(cf_weight, 4)
    return cf_weight, cbf_weight


# ─────────────────────────────────────────────────────────────────────────────
# Core blending logic
# ─────────────────────────────────────────────────────────────────────────────

def _merge_results(
    cf_recs:   list[dict],
    cbf_recs:  list[dict],
    cf_weight: float,
    cbf_weight: float,
    top_k: int,
) -> list[HybridRecommendation]:
   
    # Normalise CF scores
    cf_map: dict[str, float] = {}
    if cf_recs:
        max_cf = max(r["cf_score"] for r in cf_recs) or 1.0
        cf_map = {r["item"]: r["cf_score"] / max_cf for r in cf_recs}

    # Normalise CBF scores + build metadata lookup
    cbf_map:  dict[str, float] = {}
    cbf_meta: dict[str, dict]  = {}
    if cbf_recs:
        max_cbf = max(r["cbf_score"] for r in cbf_recs) or 1.0
        for r in cbf_recs:
            cbf_map[r["item"]]  = r["cbf_score"] / max_cbf
            cbf_meta[r["item"]] = r

    # Union of all item keys
    all_items = set(cf_map) | set(cbf_map)

    merged: list[HybridRecommendation] = []
    for item in all_items:
        cf_s  = cf_map.get(item, 0.0)
        cbf_s = cbf_map.get(item, 0.0)
        hybrid = round(cf_weight * cf_s + cbf_weight * cbf_s, 4)

        meta = cbf_meta.get(item, {})

        if cf_s > 0 and cbf_s > 0:
            source = "hybrid"
        elif cf_s > 0:
            source = "cf_only"
        else:
            source = "cbf_only"

        merged.append(HybridRecommendation(
            item         = item,
            name         = meta.get("name", ""),
            category     = meta.get("category", ""),
            cf_score     = round(cf_s, 3),
            cbf_score    = round(cbf_s, 3),
            hybrid_score = hybrid,
            source       = source,
        ))

    merged.sort(key=lambda x: x["hybrid_score"], reverse=True)
    return merged[:top_k]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_hybrid_recommendations(
    username:          str,
    current_query:     str,
    interaction_count: int | None = None,
    top_k:             int        = 5,
) -> list[HybridRecommendation]:
    """
    Return hybrid CF+CBF recommendations for a user query.

    Args:
        username:          The current user's identifier.
        current_query:     The user's search text or last message.
        interaction_count: Pre-computed count (avoids extra Qdrant call if you
                           already have it). Pass None to auto-count.
        top_k:             Number of recommendations to return.

    Returns:
        List of HybridRecommendation dicts, sorted by hybrid_score descending.
        Falls back to CBF-only on any CF error, and to empty list on total failure.
    """
 

    if interaction_count is None:
        interaction_count = _count_user_interactions(username)

    cf_weight, cbf_weight = _blend_weights(interaction_count)

    logger.debug(
        "hybrid_rec | user=%s interactions=%d cf_w=%.2f cbf_w=%.2f query='%s'",
        username, interaction_count, cf_weight, cbf_weight, current_query[:60],
    )

    # Fetch CF recommendations
    cf_recs: list[dict] = []
    if cf_weight > 0:
        try:
            from memory_history.user_memory import get_cf_recommendations
            cf_recs = get_cf_recommendations(username, current_query, top_k=top_k * 2)
        except Exception as exc:
            logger.warning("hybrid_rec | CF fetch failed: %s — falling back to CBF only", exc)
            cf_weight  = 0.0
            cbf_weight = 1.0


    # Fetch CBF recommendations
    cbf_recs: list[dict] = []
    try:

        from memory_history.content_memory import get_cbf_recommendations
        
        cbf_recs = get_cbf_recommendations(_clean_query(current_query), top_k=top_k * 2)
    except Exception as exc:
        logger.warning("hybrid_rec | CBF fetch failed: %s", exc)

    # If both are empty, return nothing gracefully
    if not cf_recs and not cbf_recs:
        logger.debug("hybrid_rec | both CF and CBF returned empty for user=%s", username)
        return []

    results = _merge_results(cf_recs, cbf_recs, cf_weight, cbf_weight, top_k)

    logger.debug(
        "hybrid_rec | user=%s -> %d hybrid recs (cf=%d cbf=%d)",
        username, len(results), len(cf_recs), len(cbf_recs),
    )
    return results


def hybrid_recs_for_state(recs: list[HybridRecommendation]) -> list[dict]:
   
    return [
        {"item": r["item"], "cf_score": r["hybrid_score"]}
        for r in recs
    ]
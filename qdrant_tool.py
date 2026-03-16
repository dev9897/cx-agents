# """
# qdrant_tool.py — Semantic search tool for the SAP agent
# """
# import os
# from langchain_core.tools import tool
# from qdrant_client import QdrantClient
# from sentence_transformers import SentenceTransformer
# from dotenv import load_dotenv

# load_dotenv()

# _qdrant   = QdrantClient(
#     url=os.getenv("QDRANT_HOST"),
#     api_key=os.getenv("QDRANT_API_KEY"),
#     timeout=30,
#     check_compatibility=False,
# )
# _embedder = SentenceTransformer("all-MiniLM-L6-v2")


# @tool
# def semantic_search_products(query: str, top_k: int = 5) -> dict:
#     """
#     Vector DB (Qdrant) mein semantic product search karo.
#     Natural language queries work karti hain jaise:
#     'wireless headphones under budget', 'camera for travel', 'budget laptop'
#     SAP keyword search se zyada smart hai yeh.
#     """
#     vector  = _embedder.encode(query).tolist()
#     results = _qdrant.search(
#         collection_name="sap_products",
#         query_vector=vector,
#         limit=top_k,
#         score_threshold=0.2,
#     )

#     if not results:
#         return {"success": True, "products": [], "message": "Koi relevant product nahi mila"}

#     return {
#         "success":  True,
#         "total":    len(results),
#         "products": [
#             {
#                 "code":    h.payload.get("code"),
#                 "name":    h.payload.get("name"),
#                 "price":   h.payload.get("price"),
#                 "stock":   h.payload.get("stock"),
#                 "summary": h.payload.get("summary", "")[:150],
#                 "score":   round(h.score, 3),
#             }
#             for h in results
#         ],
#     }









"""
qdrant_tool.py
==============
Qdrant Cloud semantic product search tool — for the SAP agent.
"""

import os
from langchain_core.tools import tool
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv

load_dotenv()

QDRANT_URL      = os.getenv("QDRANT_HOST")
QDRANT_API_KEY  = os.getenv("QDRANT_API_KEY")
COLLECTION_NAME = "sap_products"

_qdrant = QdrantClient(
    url=QDRANT_URL,
    api_key=QDRANT_API_KEY,
    timeout=30,
    check_compatibility=False,
)
_embedder = SentenceTransformer("all-MiniLM-L6-v2")


@tool
def semantic_search_products(query: str, top_k: int = 5) -> dict:
    """
    Perform semantic product search in the Qdrant vector database.
    Supports natural language queries such as:
    'wireless headphones under budget', 'camera for travel', 'budget laptop'.
    Smarter than standard SAP keyword search.
    """

    vector = _embedder.encode(query).tolist()

    results = _qdrant.search(
        collection_name=COLLECTION_NAME,
        query_vector=vector,
        limit=top_k,
        score_threshold=0.2,
    )

    if not results:
        return {
            "success": True,
            "products": [],
            "message": "No relevant products found.",
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
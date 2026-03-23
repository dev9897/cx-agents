"""
Image Search — Upload a photo and find matching products.

Two modes:
  **Local (primary)** — CLIP + Qdrant vector similarity
    1. CLIP encodes the uploaded image into an embedding vector
    2. Qdrant searches the clip_product_images collection for nearest matches
    3. Fast, private, requires transformers + torch + Qdrant

  **Cloud (fallback)** — OpenAI Vision → text search
    1. GPT-4o-mini describes the product in the uploaded image
    2. Description is used to text-search SAP Commerce
    3. No local GPU needed, requires OPENAI_API_KEY

Set IMAGE_SEARCH_PROVIDER=local|cloud|auto (default: auto)
  auto = local CLIP if available, else cloud fallback
"""

import base64
import io
import logging
import os
import uuid
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, File, HTTPException, UploadFile
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.features.registry import BaseFeature

load_dotenv()

logger = logging.getLogger("sap_agent.features.image_search")

# ── Config ───────────────────────────────────────────────────────────────────

QDRANT_URL = os.getenv("QDRANT_HOST")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
CLIP_COLLECTION = "clip_product_images"
CLIP_MODEL_NAME = os.getenv("CLIP_MODEL", "openai/clip-vit-base-patch32")
CLIP_EMBEDDING_DIM = 512

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
IMAGE_SEARCH_PROVIDER = os.getenv("IMAGE_SEARCH_PROVIDER", "auto")  # "auto", "local", "cloud"

# ── Lazy-loaded models ───────────────────────────────────────────────────────

_clip_model = None
_clip_processor = None
_clip_tokenizer = None
_qdrant = None


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from qdrant_client import QdrantClient
        _qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY,
                               timeout=30, check_compatibility=False)
    return _qdrant


def _get_clip():
    """Lazy-load CLIP model and processor."""
    global _clip_model, _clip_processor, _clip_tokenizer
    if _clip_model is None:
        try:
            from transformers import CLIPModel, CLIPProcessor, CLIPTokenizer
            import torch

            _clip_model = CLIPModel.from_pretrained(CLIP_MODEL_NAME)
            _clip_processor = CLIPProcessor.from_pretrained(CLIP_MODEL_NAME)
            _clip_tokenizer = CLIPTokenizer.from_pretrained(CLIP_MODEL_NAME)
            _clip_model.eval()
            logger.info("CLIP model loaded: %s", CLIP_MODEL_NAME)
        except ImportError:
            logger.error("transformers/torch not installed — image search unavailable")
            raise
    return _clip_model, _clip_processor, _clip_tokenizer


# ── Qdrant helpers ───────────────────────────────────────────────────────────

def ensure_clip_collection():
    """Create CLIP image collection if it doesn't exist."""
    client = _get_qdrant()
    from qdrant_client.models import Distance, VectorParams
    collections = [c.name for c in client.get_collections().collections]
    if CLIP_COLLECTION not in collections:
        client.create_collection(
            collection_name=CLIP_COLLECTION,
            vectors_config=VectorParams(size=CLIP_EMBEDDING_DIM,
                                        distance=Distance.COSINE),
        )
        logger.info("Created collection: %s", CLIP_COLLECTION)


# ── Image encoding ───────────────────────────────────────────────────────────

def _bytes_to_pil(image_bytes: bytes):
    """Open image bytes with PIL, handling edge cases from browser uploads.

    Browsers may send WebP, HEIC, or other formats that need special handling.
    Returns a PIL Image in RGB mode.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except UnidentifiedImageError:
        pass

    # Fallback: if raw bytes fail, the browser might have sent a data-URL
    # fragment or the bytes need re-wrapping.  Try common format hints.
    for fmt in ("PNG", "JPEG", "WEBP"):
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
            return img.convert("RGB")
        except Exception:
            continue

    raise ValueError(
        "Cannot decode image. Supported formats: JPEG, PNG, WebP, GIF, BMP, TIFF. "
        "Please try a different image or convert it to JPEG/PNG first."
    )


def _to_tensor(features):
    """Extract a raw tensor from CLIP output (handles different transformers versions)."""
    import torch
    if isinstance(features, torch.Tensor):
        return features
    # Newer transformers may return BaseModelOutputWithPooling
    if hasattr(features, "pooler_output") and features.pooler_output is not None:
        return features.pooler_output
    if hasattr(features, "last_hidden_state"):
        return features.last_hidden_state[:, 0, :]
    # Last resort: first element
    return features[0] if hasattr(features, "__getitem__") else features


def encode_image(image_bytes: bytes) -> list[float]:
    """Encode an image into a CLIP embedding vector."""
    import torch

    model, processor, _ = _get_clip()
    image = _bytes_to_pil(image_bytes)
    inputs = processor(images=image, return_tensors="pt")

    with torch.no_grad():
        raw = model.get_image_features(**inputs)
        image_features = _to_tensor(raw)
        image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)

    return image_features[0].cpu().numpy().tolist()


def encode_text_for_clip(text: str) -> list[float]:
    """Encode text into CLIP embedding (for text-to-image matching)."""
    import torch

    model, processor, tokenizer = _get_clip()
    inputs = tokenizer(text, return_tensors="pt", padding=True, truncation=True)

    with torch.no_grad():
        raw = model.get_text_features(**inputs)
        text_features = _to_tensor(raw)
        text_features = text_features / text_features.norm(p=2, dim=-1, keepdim=True)

    return text_features[0].cpu().numpy().tolist()


def encode_product_image_from_url(image_url: str) -> Optional[list[float]]:
    """Download and encode a product image from URL."""
    try:
        import httpx
        response = httpx.get(image_url, timeout=15, verify=False)
        if response.status_code == 200:
            return encode_image(response.content)
    except Exception as e:
        logger.warning("Failed to encode image from %s: %s", image_url, e)
    return None


# ── Local availability check ─────────────────────────────────────────────────

def _local_available() -> bool:
    """Check if local CLIP + Qdrant is usable."""
    if not QDRANT_URL:
        return False
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _use_local() -> bool:
    """Decide whether to use local CLIP search."""
    if IMAGE_SEARCH_PROVIDER == "local":
        return True
    if IMAGE_SEARCH_PROVIDER == "cloud":
        return False
    # "auto": prefer local if available
    return _local_available()


# ── Cloud fallback: OpenAI Vision → text search ─────────────────────────────

def _describe_image_cloud(image_bytes: bytes) -> Optional[str]:
    """Use GPT-4o-mini vision to describe the product in an image."""
    import httpx

    if not OPENAI_API_KEY:
        logger.warning("No OPENAI_API_KEY — cloud image search unavailable")
        return None

    b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:image/jpeg;base64,{b64}"

    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 150,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "You are a product identification assistant for an electronics store. "
                            "Describe the product in this image in 1-2 short sentences. "
                            "Focus on: product type, brand if visible, key features. "
                            "Example: 'Canon DSLR camera with telephoto lens, black body'. "
                            "If no product is visible, say 'no product detected'."
                        )},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }],
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if "no product" in text.lower():
            return None
        logger.info("Cloud image description: %s", text)
        return text
    except Exception as e:
        logger.exception("Cloud image description failed")
        return None


def _search_products_by_text(query: str, top_k: int = 6) -> dict:
    """Search products using Qdrant semantic search or SAP text search."""
    # Try Qdrant semantic search first
    try:
        from app.integrations.qdrant_client import is_qdrant_configured, semantic_search_products
        if is_qdrant_configured():
            result = semantic_search_products.invoke({"query": query, "top_k": top_k})
            if result.get("success") and result.get("products"):
                return result
    except Exception:
        pass

    # Fallback to SAP text search
    try:
        from app.integrations import sap_client
        return sap_client.search_products(query, page_size=top_k)
    except Exception as e:
        logger.exception("Text search failed for image query")
        return {"success": False, "products": [], "error": str(e)}


def _cloud_image_search(image_bytes: bytes, top_k: int = 6) -> dict:
    """Cloud fallback: describe image with GPT-4o-mini, then text-search products."""
    description = _describe_image_cloud(image_bytes)
    if not description:
        return {
            "success": False,
            "error": "Could not identify a product in this image. Try a clearer photo.",
        }

    result = _search_products_by_text(description, top_k)
    if result.get("success"):
        result["message"] = (
            f"I see: \"{description}\". "
            f"Found {result.get('total', len(result.get('products', [])))} matching products."
        )
    return result


# ── Search (dispatcher) ─────────────────────────────────────────────────────

def search_by_image(image_bytes: bytes, top_k: int = 6) -> dict:
    """Search for products matching an uploaded image.

    Uses local CLIP + Qdrant when available, falls back to cloud vision.
    """
    if _use_local():
        logger.info("Image search via local CLIP + Qdrant")
        result = _local_image_search(image_bytes, top_k)
        if result.get("success"):
            return result
        # Local failed — try cloud fallback if API key available
        if OPENAI_API_KEY:
            logger.warning("Local image search failed, attempting cloud fallback")
            return _cloud_image_search(image_bytes, top_k)
        return result
    else:
        logger.info("Image search via cloud (GPT-4o-mini vision)")
        return _cloud_image_search(image_bytes, top_k)


def _local_image_search(image_bytes: bytes, top_k: int = 6) -> dict:
    """Local CLIP + Qdrant vector similarity search."""
    try:
        vector = encode_image(image_bytes)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    try:
        client = _get_qdrant()

        results = client.query_points(
            collection_name=CLIP_COLLECTION,
            query=vector,
            limit=top_k,
            score_threshold=0.2,
            with_payload=True,
        )

        products = []
        for hit in results.points:
            products.append({
                "code": hit.payload.get("code", ""),
                "name": hit.payload.get("name", ""),
                "price": hit.payload.get("price", ""),
                "stock": hit.payload.get("stock", ""),
                "image_url": hit.payload.get("image_url", ""),
                "summary": hit.payload.get("summary", "")[:150],
                "score": round(hit.score, 3),
            })

        return {
            "success": True,
            "total": len(products),
            "products": products,
            "message": f"Found {len(products)} visually similar products."
                       if products else "No matching products found. Try a different image.",
        }
    except Exception as e:
        logger.exception("Local image search failed")
        return {"success": False, "error": f"Image search error: {e}"}


# ── Product Image Indexing ───────────────────────────────────────────────────

def index_product_images(products: list[dict]) -> int:
    """Index product images into the CLIP collection for visual search."""
    from qdrant_client.models import PointStruct

    ensure_clip_collection()
    client = _get_qdrant()
    indexed = 0

    points = []
    for product in products:
        image_url = product.get("image_url", "")
        if not image_url:
            continue

        vector = encode_product_image_from_url(image_url)
        if vector is None:
            continue

        points.append(PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={
                "code": product.get("code", ""),
                "name": product.get("name", ""),
                "price": product.get("price", ""),
                "stock": product.get("stock", ""),
                "image_url": image_url,
                "summary": product.get("summary", ""),
                "categories": product.get("categories", []),
            },
        ))

        # Batch upsert every 20 items
        if len(points) >= 20:
            client.upsert(collection_name=CLIP_COLLECTION, points=points)
            indexed += len(points)
            points = []
            logger.info("Indexed %d product images", indexed)

    if points:
        client.upsert(collection_name=CLIP_COLLECTION, points=points)
        indexed += len(points)

    logger.info("Total product images indexed: %d", indexed)
    return indexed


# ── API Routes ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/image-search", tags=["Image Search"])


class ImageSearchResponse(BaseModel):
    success: bool
    products: list[dict] = []
    total: int = 0
    message: str = ""


@router.post("", response_model=ImageSearchResponse)
async def image_search_endpoint(file: UploadFile = File(...)):
    """Upload an image to search for visually similar products."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400,
                            detail="File must be an image (JPEG, PNG, WebP)")

    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(status_code=400, detail="Image too large (max 10MB)")

    result = search_by_image(image_bytes)
    return ImageSearchResponse(**result)


@router.post("/base64", response_model=ImageSearchResponse)
async def image_search_base64(payload: dict):
    """Search by base64-encoded image (for camera capture)."""
    image_data = payload.get("image", "")
    if not image_data:
        raise HTTPException(status_code=400, detail="No image data provided")

    # Strip data URL prefix if present (e.g. "data:image/jpeg;base64,/9j/4A...")
    if "," in image_data:
        image_data = image_data.split(",", 1)[1]

    # Fix base64 padding (browsers sometimes omit trailing '=')
    padding = len(image_data) % 4
    if padding:
        image_data += "=" * (4 - padding)

    try:
        image_bytes = base64.b64decode(image_data)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    if len(image_bytes) < 100:
        raise HTTPException(status_code=400, detail="Image data too small — upload may have failed")

    result = search_by_image(image_bytes)
    return ImageSearchResponse(**result)


# ── Feature Registration ─────────────────────────────────────────────────────

class ImageSearchFeature(BaseFeature):
    @property
    def name(self) -> str:
        return "image_search"

    @property
    def description(self) -> str:
        return "Visual product search — upload photos to find matching products"

    def is_available(self) -> bool:
        # Local mode: needs Qdrant + transformers + torch
        if _local_available():
            return True
        # Cloud mode: needs OpenAI API key
        if OPENAI_API_KEY:
            logger.info("Image search using cloud fallback (GPT-4o-mini vision)")
            return True
        logger.warning("Image search unavailable: no CLIP+Qdrant and no OPENAI_API_KEY")
        return False

    def get_tools(self) -> list[BaseTool]:
        return []  # Image search is API-driven, not an agent tool

    def get_router(self) -> Optional[APIRouter]:
        return router

    def get_ui_config(self) -> dict:
        return {
            "enabled": True,
            "name": self.name,
            "accept": "image/*",
            "max_size_mb": 10,
            "camera": True,
        }

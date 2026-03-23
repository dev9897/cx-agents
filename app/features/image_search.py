"""
Image Search — Upload a photo and find matching products via CLIP + Qdrant.

Architecture:
  1. User uploads an image (camera capture or file upload)
  2. CLIP vision model encodes the image into an embedding vector
  3. Product images are pre-encoded into a separate Qdrant collection (clip_product_images)
  4. Vector similarity search returns the closest matching products
  5. Results rendered as product cards in the UI

Models:
  - CLIP (openai/clip-vit-base-patch32) via transformers — best balance of speed/quality
  - Qdrant collection: clip_product_images (512-dim CLIP vectors)
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


# ── Search ───────────────────────────────────────────────────────────────────

def search_by_image(image_bytes: bytes, top_k: int = 6) -> dict:
    """Search for products matching an uploaded image."""
    try:
        vector = encode_image(image_bytes)
    except ValueError as e:
        # User-friendly error from _bytes_to_pil
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
        logger.exception("Image search failed")
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
        if not QDRANT_URL:
            return False
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            return True
        except ImportError:
            logger.warning("transformers/torch not installed — image search unavailable")
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

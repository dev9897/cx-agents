"""
Audio Search — Record/upload audio, transcribe with Whisper, search products.

Architecture:
  1. User records audio via browser microphone or uploads an audio file
  2. Audio transcribed to text via:
     - OpenAI Whisper API (cloud, default) — fast, no GPU/ffmpeg needed
     - Local Whisper model (fallback)     — requires torch + ffmpeg
  3. Transcribed text is fed into the existing search pipeline
  4. Results returned as product cards

Set OPENAI_API_KEY in .env to use cloud transcription (recommended).
Set WHISPER_PROVIDER=local to force local inference.
"""

import logging
import os
import tempfile
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, File, HTTPException, UploadFile
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.features.registry import BaseFeature

load_dotenv()

logger = logging.getLogger("sap_agent.features.audio_search")

# ── Config ───────────────────────────────────────────────────────────────────

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHISPER_PROVIDER = os.getenv("WHISPER_PROVIDER", "auto")  # "auto", "openai_cloud", "local"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "openai/whisper-small")
SUPPORTED_AUDIO_TYPES = {
    "audio/wav", "audio/wave", "audio/x-wav",
    "audio/mpeg", "audio/mp3",
    "audio/ogg", "audio/webm",
    "audio/flac", "audio/x-flac",
    "audio/mp4", "audio/m4a",
    "video/webm",  # browser MediaRecorder often uses video/webm for audio
}
MAX_AUDIO_SIZE = 25 * 1024 * 1024  # 25MB
MAX_AUDIO_DURATION = 60  # seconds


def _use_cloud() -> bool:
    """Decide whether to use OpenAI cloud transcription."""
    if WHISPER_PROVIDER == "openai_cloud":
        return True
    if WHISPER_PROVIDER == "local":
        return False
    # "auto": use cloud if API key is available
    return bool(OPENAI_API_KEY)


# ── Cloud transcription (OpenAI Whisper API) ─────────────────────────────────

def _transcribe_cloud(audio_bytes: bytes, content_type: str) -> dict:
    """Transcribe via OpenAI Whisper API — no GPU, no ffmpeg, no torch."""
    ext = _get_extension(content_type)
    filename = f"audio{ext}"

    try:
        resp = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (filename, audio_bytes, content_type)},
            data={"model": "whisper-1", "response_format": "json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("text", "").strip()

        if not text:
            return {"success": False, "error": "Could not transcribe audio. Please try speaking more clearly."}

        return {"success": True, "transcription": text, "language": "en"}

    except httpx.HTTPStatusError as e:
        logger.error("OpenAI Whisper API error: %s %s", e.response.status_code, e.response.text)
        return {"success": False, "error": f"Cloud transcription error: HTTP {e.response.status_code}"}
    except Exception as e:
        logger.exception("Cloud transcription failed")
        return {"success": False, "error": f"Cloud transcription error: {e}"}


# ── Local transcription (transformers pipeline) ──────────────────────────────

_whisper_pipeline = None


def _get_whisper():
    """Lazy-load Whisper ASR pipeline."""
    global _whisper_pipeline
    if _whisper_pipeline is None:
        from transformers import pipeline
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        _whisper_pipeline = pipeline(
            "automatic-speech-recognition",
            model=WHISPER_MODEL,
            device=device,
            chunk_length_s=30,
            return_timestamps=False,
        )
        logger.info("Whisper model loaded: %s on %s", WHISPER_MODEL, device)
    return _whisper_pipeline


def _transcribe_local(audio_bytes: bytes, content_type: str) -> dict:
    """Transcribe using local Whisper model (requires torch + ffmpeg)."""
    try:
        pipe = _get_whisper()

        suffix = _get_extension(content_type)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            result = pipe(temp_path)
            text = result.get("text", "").strip()
        finally:
            os.unlink(temp_path)

        if not text:
            return {"success": False, "error": "Could not transcribe audio. Please try speaking more clearly."}

        return {
            "success": True,
            "transcription": text,
            "language": result.get("language", "en"),
        }
    except Exception as e:
        logger.exception("Local transcription failed")
        return {"success": False, "error": f"Transcription error: {e}"}


# ── Transcription dispatcher ─────────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, content_type: str = "audio/wav") -> dict:
    """Transcribe audio bytes to text.

    Uses cloud or local based on config.  If the primary method fails and the
    other is available, automatically falls back so the user still gets a result.
    """
    if _use_cloud():
        logger.info("Transcribing via OpenAI Whisper API (cloud)")
        result = _transcribe_cloud(audio_bytes, content_type)
        if result.get("success"):
            return result
        # Fallback: cloud failed → try local if available
        logger.warning("Cloud transcription failed, attempting local fallback")
        fallback = _transcribe_local(audio_bytes, content_type)
        return fallback if fallback.get("success") else result
    else:
        logger.info("Transcribing via local Whisper model")
        result = _transcribe_local(audio_bytes, content_type)
        if result.get("success"):
            return result
        # Fallback: local failed → try cloud if API key is available
        if OPENAI_API_KEY:
            logger.warning("Local transcription failed, attempting cloud fallback")
            fallback = _transcribe_cloud(audio_bytes, content_type)
            return fallback if fallback.get("success") else result
        return result


def _get_extension(content_type: str) -> str:
    """Map content type to file extension."""
    mapping = {
        "audio/wav": ".wav", "audio/wave": ".wav", "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
        "audio/ogg": ".ogg", "audio/webm": ".webm",
        "audio/flac": ".flac", "audio/x-flac": ".flac",
        "audio/mp4": ".m4a", "audio/m4a": ".m4a",
        "video/webm": ".webm",
    }
    return mapping.get(content_type, ".wav")


# ── Search pipeline ──────────────────────────────────────────────────────────

def audio_to_search(audio_bytes: bytes, content_type: str = "audio/wav") -> dict:
    """Full pipeline: transcribe audio → search products."""
    # Step 1: Transcribe
    transcription = transcribe_audio(audio_bytes, content_type)
    if not transcription.get("success"):
        return transcription

    query = transcription["transcription"]
    logger.info("Audio transcribed: '%s'", query)

    # Step 2: Search using existing pipelines
    search_result = _search_products(query)

    return {
        "success": True,
        "transcription": query,
        "products": search_result.get("products", []),
        "total": search_result.get("total", 0),
        "message": f"I heard: \"{query}\". {search_result.get('message', '')}".strip(),
    }


def _search_products(query: str) -> dict:
    """Search products using Qdrant semantic search (preferred) or SAP text search."""
    # Try Qdrant semantic search first
    try:
        from app.integrations.qdrant_client import is_qdrant_configured, semantic_search_products
        if is_qdrant_configured():
            result = semantic_search_products.invoke({"query": query, "top_k": 6})
            if result.get("success") and result.get("products"):
                return result
    except Exception:
        pass

    # Fallback to SAP text search
    try:
        from app.integrations import sap_client
        result = sap_client.search_products(query, page_size=6)
        return result
    except Exception as e:
        logger.exception("Product search failed for audio query")
        return {"success": False, "products": [], "error": str(e)}


# ── API Routes ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/audio-search", tags=["Audio Search"])


class AudioSearchResponse(BaseModel):
    success: bool
    transcription: str = ""
    products: list[dict] = []
    total: int = 0
    message: str = ""


class TranscriptionResponse(BaseModel):
    success: bool
    transcription: str = ""
    language: str = ""
    error: str = ""


@router.post("", response_model=AudioSearchResponse)
async def audio_search_endpoint(file: UploadFile = File(...)):
    """Upload audio to transcribe and search for products."""
    # Strip codec params (e.g. "audio/webm;codecs=opus" → "audio/webm")
    content_type = (file.content_type or "audio/wav").split(";")[0].strip()
    if content_type not in SUPPORTED_AUDIO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {content_type}. "
                   f"Supported: WAV, MP3, OGG, WebM, FLAC, M4A")

    audio_bytes = await file.read()
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        raise HTTPException(status_code=400, detail="Audio too large (max 25MB)")

    result = audio_to_search(audio_bytes, content_type)
    return AudioSearchResponse(**result)


@router.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe_endpoint(file: UploadFile = File(...)):
    """Transcribe audio only (without searching)."""
    content_type = (file.content_type or "audio/wav").split(";")[0].strip()
    audio_bytes = await file.read()

    if len(audio_bytes) > MAX_AUDIO_SIZE:
        raise HTTPException(status_code=400, detail="Audio too large (max 25MB)")

    result = transcribe_audio(audio_bytes, content_type)
    return TranscriptionResponse(**result)


# ── Feature Registration ─────────────────────────────────────────────────────

class AudioSearchFeature(BaseFeature):
    @property
    def name(self) -> str:
        return "audio_search"

    @property
    def description(self) -> str:
        return "Voice-powered product search — speak or upload audio"

    def is_available(self) -> bool:
        # Cloud mode only needs httpx (already a core dependency)
        if _use_cloud():
            return True
        # Local mode needs transformers + torch
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
            return True
        except ImportError:
            logger.warning("transformers/torch not installed and no OPENAI_API_KEY — audio search unavailable")
            return False

    def get_tools(self) -> list[BaseTool]:
        return []  # Audio search is API-driven, not an agent tool

    def get_router(self) -> Optional[APIRouter]:
        return router

    def get_ui_config(self) -> dict:
        return {
            "enabled": True,
            "name": self.name,
            "accept": ",".join(SUPPORTED_AUDIO_TYPES),
            "max_size_mb": 25,
            "microphone": True,
        }

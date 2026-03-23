"""
Feature registry — pluggable architecture for enabling/disabling feature flows.

Each feature registers itself with the registry. The registry controls:
  - Which features are active (based on env config)
  - Which API routes to mount
  - Which agent tools to include
  - Which UI components to expose
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter
from langchain_core.tools import BaseTool

load_dotenv()

logger = logging.getLogger("sap_agent.features")


@dataclass
class FeatureConfig:
    """Configuration for a single feature."""
    name: str
    enabled: bool = True
    description: str = ""


class BaseFeature(ABC):
    """Abstract base class for pluggable features."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique feature identifier."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if all dependencies (models, APIs) are available."""

    def get_tools(self) -> list[BaseTool]:
        """Return LangChain tools this feature provides. Override if needed."""
        return []

    def get_router(self) -> Optional[APIRouter]:
        """Return FastAPI router for this feature's endpoints. Override if needed."""
        return None

    def get_ui_config(self) -> dict:
        """Return UI configuration hints for the frontend."""
        return {"enabled": True, "name": self.name}


class FeatureRegistry:
    """Singleton registry for all pluggable features."""

    _instance: Optional["FeatureRegistry"] = None

    def __init__(self):
        self._features: dict[str, BaseFeature] = {}
        self._configs: dict[str, FeatureConfig] = {}
        self._load_configs()

    @classmethod
    def instance(cls) -> "FeatureRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_configs(self):
        """Load feature flags from environment."""
        self._configs = {
            "recommendations": FeatureConfig(
                name="recommendations",
                enabled=os.getenv("FEATURE_RECOMMENDATIONS", "true").lower() == "true",
                description="AI-powered product recommendations using collaborative and content filtering",
            ),
            "image_search": FeatureConfig(
                name="image_search",
                enabled=os.getenv("FEATURE_IMAGE_SEARCH", "true").lower() == "true",
                description="Visual product search — upload an image to find matching products",
            ),
            "audio_search": FeatureConfig(
                name="audio_search",
                enabled=os.getenv("FEATURE_AUDIO_SEARCH", "true").lower() == "true",
                description="Voice-powered search — speak or upload audio to search products",
            ),
        }

    def register(self, feature: BaseFeature) -> None:
        """Register a feature if it's enabled and available."""
        config = self._configs.get(feature.name)
        if config and not config.enabled:
            logger.info("Feature '%s' is disabled by config — skipping", feature.name)
            return

        if not feature.is_available():
            logger.warning("Feature '%s' dependencies not met — skipping", feature.name)
            return

        self._features[feature.name] = feature
        logger.info("Feature '%s' registered successfully", feature.name)

    def get_feature(self, name: str) -> Optional[BaseFeature]:
        return self._features.get(name)

    def is_enabled(self, name: str) -> bool:
        return name in self._features

    def get_all_tools(self) -> list[BaseTool]:
        """Collect tools from all active features."""
        tools = []
        for feature in self._features.values():
            tools.extend(feature.get_tools())
        return tools

    def get_all_routers(self) -> list[APIRouter]:
        """Collect routers from all active features."""
        routers = []
        for feature in self._features.values():
            router = feature.get_router()
            if router:
                routers.append(router)
        return routers

    def get_ui_config(self) -> dict:
        """Return UI configuration for all features."""
        return {
            name: {
                "enabled": name in self._features,
                "description": self._configs.get(name, FeatureConfig(name=name)).description,
                **(self._features[name].get_ui_config() if name in self._features else {}),
            }
            for name in self._configs
        }

    @property
    def active_features(self) -> list[str]:
        return list(self._features.keys())

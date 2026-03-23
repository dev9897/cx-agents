"""
Pluggable feature modules for the SAP Commerce Agent.

Each feature (recommendations, image search, audio search) is a self-contained
module that can be independently enabled/disabled via configuration.

Architecture:
    FeatureRegistry → registers/discovers features at startup
    BaseFeature     → abstract base that each feature implements
    Config flags    → FEATURE_RECOMMENDATIONS, FEATURE_IMAGE_SEARCH, FEATURE_AUDIO_SEARCH
"""

from app.features.registry import FeatureRegistry, BaseFeature

__all__ = ["FeatureRegistry", "BaseFeature"]

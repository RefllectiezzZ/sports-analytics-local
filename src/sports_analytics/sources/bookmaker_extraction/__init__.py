"""Bookmaker raw-response extraction profiles (verified vs example/synthetic)."""

from sports_analytics.sources.bookmaker_extraction.contracts import (
    ADAPTER_CONTRACT_SCHEMA,
    ExtractionProfile,
    ExtractionResult,
)
from sports_analytics.sources.bookmaker_extraction.registry import (
    VERIFICATION_PROCEDURE,
    get_verified_extraction_profile,
)

__all__ = [
    "ADAPTER_CONTRACT_SCHEMA",
    "ExtractionProfile",
    "ExtractionResult",
    "VERIFICATION_PROCEDURE",
    "get_verified_extraction_profile",
]

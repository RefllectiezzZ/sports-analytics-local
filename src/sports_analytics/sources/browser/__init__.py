"""Browser acquisition package."""

from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserDiagnosticReference,
    BrowserMode,
    BrowserNetworkMetadata,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.safety import (
    classify_block_signals,
    classify_https_public_url,
    validate_provider_navigation_url,
)

__all__ = [
    "BrowserAcquisitionResult",
    "BrowserBlockReason",
    "BrowserDiagnosticReference",
    "BrowserMode",
    "BrowserNetworkMetadata",
    "BrowserPageObservation",
    "BrowserResponseObservation",
    "classify_block_signals",
    "classify_https_public_url",
    "validate_provider_navigation_url",
]

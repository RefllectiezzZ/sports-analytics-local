"""Browser acquisition package."""

from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserDiagnosticReference,
    BrowserMode,
    BrowserPageObservation,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.safety import (
    classify_block_signals,
    validate_provider_navigation_url,
)

__all__ = [
    "BrowserAcquisitionResult",
    "BrowserBlockReason",
    "BrowserDiagnosticReference",
    "BrowserMode",
    "BrowserPageObservation",
    "BrowserResponseObservation",
    "classify_block_signals",
    "validate_provider_navigation_url",
]

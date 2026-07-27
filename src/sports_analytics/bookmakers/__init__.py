"""Bookmaker domain package: selection, multiples, reconciliation, and snapshots."""

from sports_analytics.bookmakers.fallback import (
    CachedSnapshotReference,
    ProviderAttemptOutcome,
    ProviderFallbackDecision,
    resolve_provider_fallback,
)
from sports_analytics.bookmakers.markets import (
    KNOWN_CANONICAL_MARKET_DEFINITION_IDS,
    map_provider_market_to_canonical,
)
from sports_analytics.bookmakers.multiples import (
    BookmakerMultiple,
    BookmakerMultipleLeg,
    CrossBookmakerSinglesComparison,
    build_same_bookmaker_multiple,
    compare_provider_multiples,
)
from sports_analytics.bookmakers.priced_quote import BookmakerPricedQuote
from sports_analytics.bookmakers.selection import (
    BookmakerSelectionPolicy,
    select_quote_pair,
)
from sports_analytics.bookmakers.status import ProviderStatusRecord, build_provider_status
from sports_analytics.bookmakers.types import (
    BOOKMAKER_SCHEMA_VERSION,
    BOOKMAKER_SNAPSHOT_TYPE,
    INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    FailureClassification,
    ProviderStatusCode,
    QuoteSelectionReason,
    SelectionMode,
)

__all__ = [
    "BOOKMAKER_SCHEMA_VERSION",
    "BOOKMAKER_SNAPSHOT_TYPE",
    "DEFAULT_BOOKMAKER_SELECTION_POLICY",
    "INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE",
    "KNOWN_CANONICAL_MARKET_DEFINITION_IDS",
    "PROVIDER_BETANO_PT",
    "PROVIDER_BETCLIC_PT",
    "BookmakerMultiple",
    "BookmakerMultipleLeg",
    "BookmakerPricedQuote",
    "BookmakerQuoteComparison",
    "BookmakerSelectionPolicy",
    "CachedSnapshotReference",
    "CrossBookmakerSinglesComparison",
    "FailureClassification",
    "ProviderAttemptOutcome",
    "ProviderFallbackDecision",
    "ProviderStatusCode",
    "ProviderStatusRecord",
    "QuoteSelectionReason",
    "SelectionMode",
    "build_provider_status",
    "build_same_bookmaker_multiple",
    "compare_provider_multiples",
    "map_provider_market_to_canonical",
    "resolve_provider_fallback",
    "select_quote_pair",
]

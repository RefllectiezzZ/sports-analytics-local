"""Bookmaker domain constants and closed enumerations for PR #11."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final

from sports_analytics.data.types import JsonValue

PROVIDER_BETANO_PT: Final[str] = "betano-pt"
PROVIDER_BETCLIC_PT: Final[str] = "betclic-pt"
SUPPORTED_BOOKMAKER_PROVIDERS: Final[tuple[str, ...]] = (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
)

INGEST_BOOKMAKER_CURRENT_ODDS_JOB_TYPE: Final[str] = "ingest.bookmaker-current-odds"
INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE: Final[str] = "ingest.bookmaker-autonomous-cycle"
BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER: Final[str] = "bookmaker-autonomous"
DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS: Final[int] = 2
BOOKMAKER_SNAPSHOT_TYPE: Final[str] = "current-bookmaker-odds"
BOOKMAKER_SCHEMA_VERSION: Final[str] = "bookmaker-canonical-v1"
BOOKMAKER_SCHEMA_VERSION_V2: Final[str] = "bookmaker-native-v2"
SUPPORTED_BOOKMAKER_SNAPSHOT_SCHEMAS: Final[tuple[str, ...]] = (
    BOOKMAKER_SCHEMA_VERSION,
    BOOKMAKER_SCHEMA_VERSION_V2,
)
BOOKMAKER_SNAPSHOT_SCHEMA_VERSION: Final[str] = BOOKMAKER_SCHEMA_VERSION

QUOTE_EQUIVALENCE_POLICY_ID: Final[str] = "bookmaker-quote-equivalence-v1"
BOOKMAKER_SELECTION_POLICY_ID: Final[str] = "bookmaker-selection-policy-v1"
BOOKMAKER_FALLBACK_POLICY_ID: Final[str] = "bookmaker-fallback-policy-v1"
BOOKMAKER_EVENT_RECONCILIATION_POLICY_ID: Final[str] = "bookmaker-event-reconciliation-v1"
BOOKMAKER_NORMALIZER_VERSION: Final[str] = "bookmaker-normalizer-v1"

DEFAULT_QUOTE_MAXIMUM_AGE_SECONDS: Final[int] = 300
DEFAULT_EVENT_START_TOLERANCE_SECONDS: Final[int] = 15 * 60

PARTITION_KEY_SPORT: Final[str] = "sport"
BOOKMAKER_COMBINED_SOURCE_NAME: Final[str] = "bookmakers"


class SelectionMode(StrEnum):
    """How a preferred/comparison bookmaker pair chooses a quote."""

    BETANO = "betano"
    BETCLIC = "betclic"
    BOTH = "both"
    BEST = "best"
    PREFERRED_UNLESS_BETTER = "preferred-unless-better"


class ProviderStatusCode(StrEnum):
    """Operational status of one bookmaker provider acquisition surface."""

    OPERATIONAL = "operational"
    PARTIAL = "partial"
    STALE = "stale"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    DRIFT_DETECTED = "drift-detected"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class FailureClassification(StrEnum):
    """How a provider acquisition failure should be treated by fallback logic."""

    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    BLOCKED = "blocked"
    NONE = "none"


class QuoteSelectionReason(StrEnum):
    """Auditable reason codes for bookmaker quote selection decisions."""

    PREFERRED_ONLY = "preferred-only"
    COMPARISON_FALLBACK = "comparison-fallback"
    HIGHER_ODDS = "higher-odds"
    EQUAL_ODDS_PREFERRED = "equal-odds-preferred"
    PREFERRED_RETAINED_STALE_COMPARISON = "preferred-retained-stale-comparison"
    BOTH_RETAINED = "both-retained"
    NEITHER_AVAILABLE = "neither-available"
    MODE_FORCED_PREFERRED = "mode-forced-preferred"
    MODE_FORCED_COMPARISON = "mode-forced-comparison"
    BEST_SELECTED = "best-selected"
    INCOMPLETE_MULTIPLE = "incomplete-multiple"
    SAME_BOOKMAKER_MULTIPLE = "same-bookmaker-multiple"
    CACHED_STALE_PRESERVED = "cached-stale-preserved"
    PROVIDER_UNAVAILABLE = "provider-unavailable"


@dataclass(frozen=True, slots=True)
class BookmakerIngestionResult:
    """Concise typed summary returned by bookmaker acquisition."""

    provider_id: str
    sport: str
    acquisition_cycle_id: str
    adapter_version: str
    status: str
    observed_at_utc: str
    snapshot_id: str | None
    snapshot_reused: bool
    block_reason: str | None
    failure_classification: str
    events_observed: int
    valid_quotes_observed: int
    unresolved_events: int
    rejected_markets: int
    warnings: tuple[str, ...]
    drift_codes: tuple[str, ...]
    response_observation_count: int = 0
    recognized_profile_response_count: int = 0

    def to_json(self) -> dict[str, JsonValue]:
        """Return a canonical JSON-compatible mapping."""
        payload = dict(asdict(self))
        payload["warnings"] = list(self.warnings)
        payload["drift_codes"] = list(self.drift_codes)
        return payload

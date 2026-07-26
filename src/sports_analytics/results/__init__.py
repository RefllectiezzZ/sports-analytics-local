"""Canonical result contracts and verified immutable result snapshots."""

from sports_analytics.results.contracts import (
    CanonicalResult,
    EventResultStatus,
    MarketOutcome,
    ParticipantResult,
    ResultInputSnapshot,
    build_canonical_result,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import (
    RESULT_SNAPSHOT_SCHEMA_VERSION,
    VerifiedResultSnapshot,
    load_result_snapshot,
    publish_result_snapshot,
)

__all__ = [
    "RESULT_SNAPSHOT_SCHEMA_VERSION",
    "CanonicalResult",
    "EventResultStatus",
    "MarketOutcome",
    "ParticipantResult",
    "ResultInputSnapshot",
    "VerifiedResultSnapshot",
    "build_canonical_result",
    "build_football_full_match_1x2_result",
    "load_result_snapshot",
    "publish_result_snapshot",
]

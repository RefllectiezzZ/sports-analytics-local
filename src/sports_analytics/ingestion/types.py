"""Ingestion job type constants and football result helpers.

``PublishedSnapshot`` is deliberately generic. The football-specific result shape
lives here, in the ingestion package, and interprets generic dataset counts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final

from sports_analytics.data.types import JsonValue
from sports_analytics.markets.schemas import DATASET_MARKET_QUOTES
from sports_analytics.snapshots.types import PublishedSnapshot
from sports_analytics.sports.football.contracts import (
    DATASET_POST_MATCH_STATISTICS,
    PARTITION_KEY_COMPETITION,
    PARTITION_KEY_SEASON,
)
from sports_analytics.sports.schemas import DATASET_EVENTS, DATASET_PARTICIPANTS

INGEST_FOOTBALL_DATA_CSV_JOB_TYPE: Final[str] = "ingest.football-data-csv"
DEFAULT_INGESTION_MAXIMUM_ATTEMPTS: Final[int] = 3


@dataclass(frozen=True, slots=True)
class FootballIngestionResult:
    """Concise typed summary returned by football ingestion."""

    snapshot_id: str
    snapshot_status: str
    snapshot_reused: bool
    snapshot_relative_path: str
    snapshot_type: str
    schema_version: str
    source_name: str
    source_version: str
    source_file_sha256: str
    competition_id: str
    season_label: str
    season_id: str
    events_count: int
    participants_count: int
    market_quotes_count: int
    post_match_statistics_count: int
    unresolved_event_count: int

    def to_json(self) -> dict[str, JsonValue]:
        """Return a canonical JSON-compatible mapping."""
        return dict(asdict(self))


def published_to_result(published: PublishedSnapshot) -> FootballIngestionResult:
    """Convert a generic published snapshot into the football handler/CLI result."""
    domain_metadata = published.domain_metadata
    season_id = domain_metadata.get("season_id")
    quality = published.metrics.quality_mapping()
    return FootballIngestionResult(
        snapshot_id=published.snapshot_id,
        snapshot_status=published.snapshot_status.value,
        snapshot_reused=published.snapshot_reused,
        snapshot_relative_path=published.snapshot_relative_path,
        snapshot_type=published.snapshot_type,
        schema_version=published.schema_version,
        source_name=published.source_name,
        source_version=published.source_version,
        source_file_sha256=published.raw_artifact_sha256,
        competition_id=published.partition_value(PARTITION_KEY_COMPETITION),
        season_label=published.partition_value(PARTITION_KEY_SEASON),
        season_id=season_id if isinstance(season_id, str) else "",
        events_count=published.row_count(DATASET_EVENTS),
        participants_count=published.row_count(DATASET_PARTICIPANTS),
        market_quotes_count=published.row_count(DATASET_MARKET_QUOTES),
        post_match_statistics_count=published.row_count(DATASET_POST_MATCH_STATISTICS),
        unresolved_event_count=quality.get("unresolved_event_count", 0),
    )

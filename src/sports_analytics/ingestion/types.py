"""Ingestion job type constants and result helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final

from sports_analytics.data.types import JsonValue
from sports_analytics.snapshots.types import PublishedSnapshot

INGEST_FOOTBALL_DATA_CSV_JOB_TYPE: Final[str] = "ingest.football-data-csv"
DEFAULT_INGESTION_MAXIMUM_ATTEMPTS: Final[int] = 3


@dataclass(frozen=True, slots=True)
class FootballIngestionResult:
    """Concise typed summary returned by football ingestion."""

    snapshot_id: str
    snapshot_status: str
    snapshot_reused: bool
    snapshot_relative_path: str
    source_name: str
    source_version: str
    source_file_sha256: str
    competition_id: str
    season_id: str
    games_count: int
    teams_count: int
    odds_quotes_count: int
    statistics_rows_count: int
    duplicate_rows_discarded: int
    warnings_count: int

    def to_json(self) -> dict[str, JsonValue]:
        """Return a canonical JSON-compatible mapping."""
        return dict(asdict(self))


def published_to_result(published: PublishedSnapshot) -> FootballIngestionResult:
    """Convert a published snapshot into the handler/CLI result shape."""
    return FootballIngestionResult(
        snapshot_id=published.snapshot_id,
        snapshot_status=published.snapshot_status.value,
        snapshot_reused=published.snapshot_reused,
        snapshot_relative_path=published.snapshot_relative_path,
        source_name=published.source_name,
        source_version=published.source_version,
        source_file_sha256=published.source_file_sha256,
        competition_id=published.competition_id,
        season_id=published.season_id,
        games_count=published.games_count,
        teams_count=published.teams_count,
        odds_quotes_count=published.odds_quotes_count,
        statistics_rows_count=published.statistics_rows_count,
        duplicate_rows_discarded=published.duplicate_rows_discarded,
        warnings_count=published.warnings_count,
    )

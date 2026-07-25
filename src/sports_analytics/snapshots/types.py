"""Snapshot publication result types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sports_analytics.data.types import SnapshotStatus


@dataclass(frozen=True, slots=True)
class PublishedSnapshot:
    """Result of publishing or reusing an immutable football snapshot."""

    snapshot_id: str
    snapshot_status: SnapshotStatus
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
    manifest_checksum_sha256: str
    source_observed_at_utc: datetime

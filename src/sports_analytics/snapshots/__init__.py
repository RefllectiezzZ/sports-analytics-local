"""Immutable Parquet snapshot preparation, publication, and verification."""

from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
)

__all__ = [
    "FOOTBALL_CANONICAL_SCHEMA_VERSION",
    "FOOTBALL_INGESTION_SNAPSHOT_TYPE",
]

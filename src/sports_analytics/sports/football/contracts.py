"""Versioned football canonical contract constants."""

from __future__ import annotations

from typing import Final

FOOTBALL_CANONICAL_SCHEMA_VERSION: Final[str] = "football-canonical-v1"
FOOTBALL_INGESTION_SNAPSHOT_TYPE: Final[str] = "football-ingestion"
FOOTBALL_PARSER_VERSION: Final[str] = "football-data-csv-parser-v1"
FOOTBALL_NORMALIZER_VERSION: Final[str] = "football-normalizer-v1"

DATASET_COMPETITIONS: Final[str] = "competitions"
DATASET_SEASONS: Final[str] = "seasons"
DATASET_TEAMS: Final[str] = "teams"
DATASET_GAMES: Final[str] = "games"
DATASET_ODDS_1X2: Final[str] = "odds_1x2"
DATASET_POST_MATCH_STATISTICS: Final[str] = "post_match_statistics"

CANONICAL_DATASETS: Final[tuple[str, ...]] = (
    DATASET_COMPETITIONS,
    DATASET_SEASONS,
    DATASET_TEAMS,
    DATASET_GAMES,
    DATASET_ODDS_1X2,
    DATASET_POST_MATCH_STATISTICS,
)

PARQUET_FILENAMES: Final[dict[str, str]] = {
    DATASET_COMPETITIONS: "competitions.parquet",
    DATASET_SEASONS: "seasons.parquet",
    DATASET_TEAMS: "teams.parquet",
    DATASET_GAMES: "games.parquet",
    DATASET_ODDS_1X2: "odds_1x2.parquet",
    DATASET_POST_MATCH_STATISTICS: "post_match_statistics.parquet",
}

MANIFEST_FILENAME: Final[str] = "manifest.json"
MANIFEST_VERSION: Final[str] = "snapshot-manifest-v1"

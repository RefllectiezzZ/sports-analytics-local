"""Versioned football ingestion contract constants.

The football canonical schema version identifies the football projection of the
shared canonical contracts (participants, events, reconciliations, market quotes)
plus the football-specific post-match statistics dataset.
"""

from __future__ import annotations

from typing import Final

FOOTBALL_CANONICAL_SCHEMA_VERSION: Final[str] = "football-canonical-v2"
FOOTBALL_INGESTION_SNAPSHOT_TYPE: Final[str] = "football-ingestion"
FOOTBALL_PARSER_VERSION: Final[str] = "football-data-csv-parser-v1"
FOOTBALL_NORMALIZER_VERSION: Final[str] = "football-normalizer-v2"

DATASET_POST_MATCH_STATISTICS: Final[str] = "post_match_statistics"

PARTITION_KEY_COMPETITION: Final[str] = "competition_id"
PARTITION_KEY_SEASON: Final[str] = "season_label"

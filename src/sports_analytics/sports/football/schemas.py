"""Explicit PyArrow schemas for football-canonical-v1 Parquet datasets."""

from __future__ import annotations

import hashlib
from typing import Final

import pyarrow as pa

from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.types import JsonValue
from sports_analytics.sports.football.contracts import (
    DATASET_COMPETITIONS,
    DATASET_GAMES,
    DATASET_ODDS_1X2,
    DATASET_POST_MATCH_STATISTICS,
    DATASET_SEASONS,
    DATASET_TEAMS,
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
)

PROJECT_NAME: Final[str] = "sports-analytics-local"
SPORT_CODE: Final[str] = "football"
SOURCE_ADAPTER: Final[str] = "football-data-co-uk"

# Odds decimal128: precision 10, scale 4 supports values such as 1.0100 .. 999999.9999
ODDS_DECIMAL_PRECISION: Final[int] = 10
ODDS_DECIMAL_SCALE: Final[int] = 4


def _schema_metadata(dataset_name: str) -> dict[bytes, bytes]:
    return {
        b"dataset": dataset_name.encode("utf-8"),
        b"schema_version": FOOTBALL_CANONICAL_SCHEMA_VERSION.encode("utf-8"),
        b"sport": SPORT_CODE.encode("utf-8"),
        b"source_adapter": SOURCE_ADAPTER.encode("utf-8"),
        b"project_name": PROJECT_NAME.encode("utf-8"),
    }


def competitions_schema() -> pa.Schema:
    """Return the competitions Parquet schema."""
    return pa.schema(
        [
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("sport_code", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("display_name", pa.string(), nullable=False),
            pa.field("country_code", pa.string(), nullable=False),
            pa.field("competition_type", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("source_name", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("source_competition_code", pa.string(), nullable=False),
            pa.field("timezone", pa.string(), nullable=False),
            pa.field("schema_version", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        ],
        metadata=_schema_metadata(DATASET_COMPETITIONS),
    )


def seasons_schema() -> pa.Schema:
    """Return the seasons Parquet schema."""
    return pa.schema(
        [
            pa.field("season_id", pa.string(), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("label", pa.string(), nullable=False),
            pa.field("start_year", pa.int16(), nullable=False),
            pa.field("end_year", pa.int16(), nullable=False),
            pa.field("source_season_code", pa.string(), nullable=False),
            pa.field("schema_version", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        ],
        metadata=_schema_metadata(DATASET_SEASONS),
    )


def teams_schema() -> pa.Schema:
    """Return the teams Parquet schema."""
    return pa.schema(
        [
            pa.field("team_id", pa.string(), nullable=False),
            pa.field("sport_code", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("source_name", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("source_team_key", pa.string(), nullable=False),
            pa.field("display_name", pa.string(), nullable=False),
            pa.field("normalized_name", pa.string(), nullable=False),
            pa.field("schema_version", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        ],
        metadata=_schema_metadata(DATASET_TEAMS),
    )


def games_schema() -> pa.Schema:
    """Return the games Parquet schema."""
    return pa.schema(
        [
            pa.field("game_id", pa.string(), nullable=False),
            pa.field("sport_code", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("competition_id", pa.string(), nullable=False),
            pa.field("season_id", pa.string(), nullable=False),
            pa.field("source_name", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("source_game_key", pa.string(), nullable=False),
            pa.field("source_row_number", pa.int32(), nullable=False),
            pa.field("event_date", pa.date32(), nullable=False),
            pa.field("scheduled_start_utc", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field(
                "start_time_precision",
                pa.dictionary(pa.int8(), pa.string()),
                nullable=False,
            ),
            pa.field("status", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("home_team_id", pa.string(), nullable=False),
            pa.field("away_team_id", pa.string(), nullable=False),
            pa.field("full_time_home_goals", pa.int16(), nullable=True),
            pa.field("full_time_away_goals", pa.int16(), nullable=True),
            pa.field("full_time_result", pa.dictionary(pa.int8(), pa.string()), nullable=True),
            pa.field("half_time_home_goals", pa.int16(), nullable=True),
            pa.field("half_time_away_goals", pa.int16(), nullable=True),
            pa.field("half_time_result", pa.dictionary(pa.int8(), pa.string()), nullable=True),
            pa.field("source_observed_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("schema_version", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        ],
        metadata=_schema_metadata(DATASET_GAMES),
    )


def odds_1x2_schema() -> pa.Schema:
    """Return the odds_1x2 Parquet schema."""
    return pa.schema(
        [
            pa.field("quote_id", pa.string(), nullable=False),
            pa.field("game_id", pa.string(), nullable=False),
            pa.field("market_type", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("selection", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("provider_type", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("provider_id", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("quote_phase", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field(
                "decimal_odds",
                pa.decimal128(ODDS_DECIMAL_PRECISION, ODDS_DECIMAL_SCALE),
                nullable=False,
            ),
            pa.field("source_column", pa.string(), nullable=False),
            pa.field("quoted_at_utc", pa.timestamp("us", tz="UTC"), nullable=True),
            pa.field("source_observed_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field(
                "quote_timestamp_precision",
                pa.dictionary(pa.int8(), pa.string()),
                nullable=False,
            ),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("quality_status", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("quality_reason", pa.string(), nullable=True),
            pa.field("schema_version", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        ],
        metadata=_schema_metadata(DATASET_ODDS_1X2),
    )


def post_match_statistics_schema() -> pa.Schema:
    """Return the post_match_statistics Parquet schema."""
    return pa.schema(
        [
            pa.field("game_id", pa.string(), nullable=False),
            pa.field("referee", pa.string(), nullable=True),
            pa.field("home_shots", pa.int16(), nullable=True),
            pa.field("away_shots", pa.int16(), nullable=True),
            pa.field("home_shots_on_target", pa.int16(), nullable=True),
            pa.field("away_shots_on_target", pa.int16(), nullable=True),
            pa.field("home_corners", pa.int16(), nullable=True),
            pa.field("away_corners", pa.int16(), nullable=True),
            pa.field("home_fouls", pa.int16(), nullable=True),
            pa.field("away_fouls", pa.int16(), nullable=True),
            pa.field("home_yellow_cards", pa.int16(), nullable=True),
            pa.field("away_yellow_cards", pa.int16(), nullable=True),
            pa.field("home_red_cards", pa.int16(), nullable=True),
            pa.field("away_red_cards", pa.int16(), nullable=True),
            pa.field("availability_stage", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("source_observed_at_utc", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("schema_version", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        ],
        metadata=_schema_metadata(DATASET_POST_MATCH_STATISTICS),
    )


def _build_schema_by_dataset() -> dict[str, pa.Schema]:
    return {
        DATASET_COMPETITIONS: competitions_schema(),
        DATASET_SEASONS: seasons_schema(),
        DATASET_TEAMS: teams_schema(),
        DATASET_GAMES: games_schema(),
        DATASET_ODDS_1X2: odds_1x2_schema(),
        DATASET_POST_MATCH_STATISTICS: post_match_statistics_schema(),
    }


SCHEMA_BY_DATASET: Final[dict[str, pa.Schema]] = _build_schema_by_dataset()


def schema_fingerprint(schema: pa.Schema) -> str:
    """Return a deterministic SHA-256 fingerprint of a logical Arrow schema."""
    fields: list[JsonValue] = []
    for field in schema:
        fields.append(
            {
                "name": field.name,
                "nullable": field.nullable,
                "type": str(field.type),
            }
        )
    metadata = {
        key.decode("utf-8"): value.decode("utf-8") for key, value in (schema.metadata or {}).items()
    }
    payload: dict[str, JsonValue] = {
        "fields": fields,
        "metadata": {key: metadata[key] for key in sorted(metadata)},
    }
    canonical = dumps_canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_schema(dataset_name: str) -> pa.Schema:
    """Return the static schema for a named football dataset."""
    try:
        return SCHEMA_BY_DATASET[dataset_name]
    except KeyError as exc:
        msg = f"unknown football dataset schema: {dataset_name}"
        raise KeyError(msg) from exc

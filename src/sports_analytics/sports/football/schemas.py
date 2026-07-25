"""Explicit PyArrow schemas for football-canonical-v1 Parquet datasets."""

from __future__ import annotations

import hashlib
from typing import Final

import pyarrow as pa

from sports_analytics.data.codec import dumps_canonical_json
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
            ("competition_id", pa.string()),
            ("sport_code", pa.dictionary(pa.int8(), pa.string())),
            ("display_name", pa.string()),
            ("country_code", pa.string()),
            ("competition_type", pa.dictionary(pa.int8(), pa.string())),
            ("source_name", pa.dictionary(pa.int8(), pa.string())),
            ("source_competition_code", pa.string()),
            ("timezone", pa.string()),
            ("schema_version", pa.dictionary(pa.int8(), pa.string())),
        ],
        metadata=_schema_metadata(DATASET_COMPETITIONS),
    )


def seasons_schema() -> pa.Schema:
    """Return the seasons Parquet schema."""
    return pa.schema(
        [
            ("season_id", pa.string()),
            ("competition_id", pa.string()),
            ("label", pa.string()),
            ("start_year", pa.int16()),
            ("end_year", pa.int16()),
            ("source_season_code", pa.string()),
            ("schema_version", pa.dictionary(pa.int8(), pa.string())),
        ],
        metadata=_schema_metadata(DATASET_SEASONS),
    )


def teams_schema() -> pa.Schema:
    """Return the teams Parquet schema."""
    return pa.schema(
        [
            ("team_id", pa.string()),
            ("sport_code", pa.dictionary(pa.int8(), pa.string())),
            ("source_name", pa.dictionary(pa.int8(), pa.string())),
            ("source_team_key", pa.string()),
            ("display_name", pa.string()),
            ("normalized_name", pa.string()),
            ("schema_version", pa.dictionary(pa.int8(), pa.string())),
        ],
        metadata=_schema_metadata(DATASET_TEAMS),
    )


def games_schema() -> pa.Schema:
    """Return the games Parquet schema."""
    return pa.schema(
        [
            ("game_id", pa.string()),
            ("sport_code", pa.dictionary(pa.int8(), pa.string())),
            ("competition_id", pa.string()),
            ("season_id", pa.string()),
            ("source_name", pa.dictionary(pa.int8(), pa.string())),
            ("source_game_key", pa.string()),
            ("source_row_number", pa.int32()),
            ("event_date", pa.date32()),
            ("scheduled_start_utc", pa.timestamp("us", tz="UTC")),
            ("start_time_precision", pa.dictionary(pa.int8(), pa.string())),
            ("status", pa.dictionary(pa.int8(), pa.string())),
            ("home_team_id", pa.string()),
            ("away_team_id", pa.string()),
            ("full_time_home_goals", pa.int16()),
            ("full_time_away_goals", pa.int16()),
            ("full_time_result", pa.dictionary(pa.int8(), pa.string())),
            ("half_time_home_goals", pa.int16()),
            ("half_time_away_goals", pa.int16()),
            ("half_time_result", pa.dictionary(pa.int8(), pa.string())),
            ("source_observed_at_utc", pa.timestamp("us", tz="UTC")),
            ("source_file_sha256", pa.string()),
            ("schema_version", pa.dictionary(pa.int8(), pa.string())),
        ],
        metadata=_schema_metadata(DATASET_GAMES),
    )


def odds_1x2_schema() -> pa.Schema:
    """Return the odds_1x2 Parquet schema."""
    return pa.schema(
        [
            ("quote_id", pa.string()),
            ("game_id", pa.string()),
            ("market_type", pa.dictionary(pa.int8(), pa.string())),
            ("selection", pa.dictionary(pa.int8(), pa.string())),
            ("provider_type", pa.dictionary(pa.int8(), pa.string())),
            ("provider_id", pa.dictionary(pa.int8(), pa.string())),
            ("quote_phase", pa.dictionary(pa.int8(), pa.string())),
            ("decimal_odds", pa.decimal128(ODDS_DECIMAL_PRECISION, ODDS_DECIMAL_SCALE)),
            ("source_column", pa.string()),
            ("quoted_at_utc", pa.timestamp("us", tz="UTC")),
            ("source_observed_at_utc", pa.timestamp("us", tz="UTC")),
            ("quote_timestamp_precision", pa.dictionary(pa.int8(), pa.string())),
            ("source_file_sha256", pa.string()),
            ("quality_status", pa.dictionary(pa.int8(), pa.string())),
            ("quality_reason", pa.string()),
            ("schema_version", pa.dictionary(pa.int8(), pa.string())),
        ],
        metadata=_schema_metadata(DATASET_ODDS_1X2),
    )


def post_match_statistics_schema() -> pa.Schema:
    """Return the post_match_statistics Parquet schema."""
    return pa.schema(
        [
            ("game_id", pa.string()),
            ("referee", pa.string()),
            ("home_shots", pa.int16()),
            ("away_shots", pa.int16()),
            ("home_shots_on_target", pa.int16()),
            ("away_shots_on_target", pa.int16()),
            ("home_corners", pa.int16()),
            ("away_corners", pa.int16()),
            ("home_fouls", pa.int16()),
            ("away_fouls", pa.int16()),
            ("home_yellow_cards", pa.int16()),
            ("away_yellow_cards", pa.int16()),
            ("home_red_cards", pa.int16()),
            ("away_red_cards", pa.int16()),
            ("availability_stage", pa.dictionary(pa.int8(), pa.string())),
            ("source_observed_at_utc", pa.timestamp("us", tz="UTC")),
            ("source_file_sha256", pa.string()),
            ("schema_version", pa.dictionary(pa.int8(), pa.string())),
        ],
        metadata=_schema_metadata(DATASET_POST_MATCH_STATISTICS),
    )


SCHEMA_BY_DATASET: Final[dict[str, pa.Schema]] = {
    DATASET_COMPETITIONS: competitions_schema(),
    DATASET_SEASONS: seasons_schema(),
    DATASET_TEAMS: teams_schema(),
    DATASET_GAMES: games_schema(),
    DATASET_ODDS_1X2: odds_1x2_schema(),
    DATASET_POST_MATCH_STATISTICS: post_match_statistics_schema(),
}


def schema_fingerprint(schema: pa.Schema) -> str:
    """Return a deterministic SHA-256 fingerprint of a logical Arrow schema."""
    fields: list[dict[str, object]] = []
    for field in schema:
        fields.append(
            {
                "name": field.name,
                "nullable": field.nullable,
                "type": str(field.type),
            }
        )
    metadata = {
        key.decode("utf-8"): value.decode("utf-8")
        for key, value in (schema.metadata or {}).items()
    }
    payload = {
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

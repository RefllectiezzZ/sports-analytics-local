"""Section 55: Arrow/Parquet schema tests for football snapshots."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.snapshots import parquet
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv
from sports_analytics.sports.football import schemas
from sports_analytics.sports.football.contracts import (
    CANONICAL_DATASETS,
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    PARQUET_FILENAMES,
)
from sports_analytics.sports.football.normalization import normalize_football_rows

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
SYNTHETIC_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)


def _normalized_bundle():
    parsed = parse_football_data_csv(SYNTHETIC_CSV, expected_division_code="E0")
    return normalize_football_rows(
        rows=list(parsed.rows),
        competition_id="eng-premier-league",
        competition_display_name="Premier League",
        country_code="ENG",
        source_competition_code="E0",
        timezone_name="Europe/London",
        season_label="2023-2024",
        start_year=2023,
        end_year=2024,
        source_season_code="2324",
        source_name="football-data-co-uk",
        source_file_sha256=hashlib.sha256(SYNTHETIC_CSV).hexdigest(),
        source_observed_at_utc=OBSERVED_AT,
    )


def test_schema_registry_exposes_all_canonical_datasets_with_metadata() -> None:
    assert tuple(schemas.SCHEMA_BY_DATASET) == CANONICAL_DATASETS

    for dataset_name in CANONICAL_DATASETS:
        schema = schemas.dataset_schema(dataset_name)
        metadata = schema.metadata or {}

        assert metadata[b"dataset"] == dataset_name.encode("utf-8")
        assert metadata[b"schema_version"] == FOOTBALL_CANONICAL_SCHEMA_VERSION.encode("utf-8")
        assert metadata[b"sport"] == b"football"
        assert metadata[b"source_adapter"] == b"football-data-co-uk"
        assert metadata[b"project_name"] == b"sports-analytics-local"
        assert schemas.schema_fingerprint(schema) == schemas.schema_fingerprint(
            schemas.dataset_schema(dataset_name)
        )
        assert len(schemas.schema_fingerprint(schema)) == 64


def test_schema_field_types_are_explicit_for_key_columns() -> None:
    assert schemas.competitions_schema().field("sport_code").type == pa.dictionary(
        pa.int8(),
        pa.string(),
    )
    assert schemas.games_schema().field("event_date").type == pa.date32()
    assert schemas.games_schema().field("scheduled_start_utc").type == pa.timestamp(
        "us",
        tz="UTC",
    )
    assert schemas.games_schema().field("source_row_number").type == pa.int32()
    assert schemas.odds_1x2_schema().field("decimal_odds").type == pa.decimal128(
        schemas.ODDS_DECIMAL_PRECISION,
        schemas.ODDS_DECIMAL_SCALE,
    )
    assert schemas.post_match_statistics_schema().field("availability_stage").type == pa.dictionary(
        pa.int8(),
        pa.string(),
    )


def test_schema_field_nullability_matches_contract() -> None:
    nullable_by_dataset = {
        "competitions": set(),
        "seasons": set(),
        "teams": set(),
        "games": {
            "scheduled_start_utc",
            "full_time_home_goals",
            "full_time_away_goals",
            "full_time_result",
            "half_time_home_goals",
            "half_time_away_goals",
            "half_time_result",
        },
        "odds_1x2": {"quoted_at_utc", "quality_reason"},
        "post_match_statistics": {
            "referee",
            "home_shots",
            "away_shots",
            "home_shots_on_target",
            "away_shots_on_target",
            "home_corners",
            "away_corners",
            "home_fouls",
            "away_fouls",
            "home_yellow_cards",
            "away_yellow_cards",
            "home_red_cards",
            "away_red_cards",
        },
    }
    for dataset_name, nullable_fields in nullable_by_dataset.items():
        schema = schemas.dataset_schema(dataset_name)
        assert {field.name for field in schema if field.nullable} == nullable_fields


def test_dataset_schema_rejects_unknown_dataset() -> None:
    with pytest.raises(KeyError, match="unknown football dataset schema"):
        schemas.dataset_schema("not-a-dataset")


def test_bundle_to_tables_uses_static_schemas_and_expected_row_counts() -> None:
    tables = parquet.bundle_to_tables(_normalized_bundle())

    assert tuple(tables) == CANONICAL_DATASETS
    assert tables["competitions"].num_rows == 1
    assert tables["seasons"].num_rows == 1
    assert tables["teams"].num_rows == 2
    assert tables["games"].num_rows == 1
    assert tables["odds_1x2"].num_rows == 0
    assert tables["post_match_statistics"].num_rows == 0
    for dataset_name, table in tables.items():
        assert table.schema == schemas.dataset_schema(dataset_name)


def test_write_bundle_parquet_files_writes_exact_immutable_file_set(tmp_path: Path) -> None:
    file_meta = parquet.write_bundle_parquet_files(tmp_path, _normalized_bundle())

    assert set(file_meta) == set(CANONICAL_DATASETS)
    assert {path.name for path in tmp_path.iterdir()} == set(PARQUET_FILENAMES.values())
    for dataset_name in CANONICAL_DATASETS:
        filename = PARQUET_FILENAMES[dataset_name]
        path = tmp_path / filename
        digest, size = parquet.file_sha256_and_size(path)
        assert file_meta[dataset_name]["relative_filename"] == filename
        assert file_meta[dataset_name]["sha256"] == digest
        assert file_meta[dataset_name]["byte_count"] == size
        assert file_meta[dataset_name]["schema_fingerprint"] == schemas.schema_fingerprint(
            schemas.dataset_schema(dataset_name)
        )
        parquet.verify_parquet_file(
            path,
            expected_schema=schemas.dataset_schema(dataset_name),
            expected_rows=int(file_meta[dataset_name]["row_count"]),
        )


def test_write_bundle_parquet_files_rejects_unexpected_preexisting_files(tmp_path: Path) -> None:
    (tmp_path / "unexpected.txt").write_text("not part of a snapshot", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="unexpected files"):
        parquet.write_bundle_parquet_files(tmp_path, _normalized_bundle())


def test_verify_parquet_file_rejects_row_count_mismatch(tmp_path: Path) -> None:
    file_meta = parquet.write_bundle_parquet_files(tmp_path, _normalized_bundle())
    games_path = tmp_path / str(file_meta["games"]["relative_filename"])

    with pytest.raises(SnapshotIntegrityError, match="row count mismatch"):
        parquet.verify_parquet_file(
            games_path,
            expected_schema=schemas.dataset_schema("games"),
            expected_rows=2,
        )


def test_verify_parquet_file_rejects_pandas_metadata(tmp_path: Path) -> None:
    path = tmp_path / "games.parquet"
    schema = schemas.dataset_schema("games").with_metadata({b"pandas": b"{}"})
    table = pa.Table.from_pylist([], schema=schema)
    pq.write_table(table, path)

    with pytest.raises(SnapshotIntegrityError, match="pandas metadata"):
        parquet.verify_parquet_file(
            path,
            expected_schema=schemas.dataset_schema("games"),
            expected_rows=0,
        )


def test_verify_parquet_file_rejects_absolute_path_metadata(tmp_path: Path) -> None:
    path = tmp_path / "games.parquet"
    schema = schemas.dataset_schema("games").with_metadata({b"source_path": b"/tmp/source.csv"})
    table = pa.Table.from_pylist([], schema=schema)
    pq.write_table(table, path)

    with pytest.raises(SnapshotIntegrityError, match="absolute path metadata"):
        parquet.verify_parquet_file(
            path,
            expected_schema=schemas.dataset_schema("games"),
            expected_rows=0,
        )

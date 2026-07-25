"""Football snapshot Arrow schema contracts and Parquet write/verify tests.

The expected field tables, nullable field sets, and schema fingerprints below are
written out literally: any schema change must be a deliberate edit here, because a
silent contract drift would otherwise invalidate every published snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.snapshots.arrow import (
    LINE_DECIMAL_PRECISION,
    LINE_DECIMAL_SCALE,
    PRICE_DECIMAL_PRECISION,
    PRICE_DECIMAL_SCALE,
    schema_fingerprint,
)
from sports_analytics.snapshots.parquet import (
    verify_parquet_file,
    write_parquet_file,
    write_suite_parquet_files,
)
from sports_analytics.sports.football.schemas import (
    CANONICAL_DATASETS,
    PARQUET_FILENAMES,
    bundle_to_tables,
    dataset_schema,
    football_schema_fingerprints,
    football_snapshot_suite,
)
from tests.helpers_snapshots import SYNTHETIC_CSV, SYNTHETIC_CSV_WITH_ODDS, build_spec, build_tables

DICTIONARY_STRING = "dictionary<values=string, indices=int8, ordered=0>"
TIMESTAMP_UTC = "timestamp[us, tz=UTC]"

EXPECTED_DATASETS = (
    "competitions",
    "seasons",
    "participants",
    "source_participants",
    "participant_reconciliations",
    "events",
    "source_events",
    "event_reconciliations",
    "market_quotes",
    "post_match_statistics",
)

EXPECTED_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "competitions": [
        ("competition_id", "string", False),
        ("sport_code", DICTIONARY_STRING, False),
        ("display_name", "string", False),
        ("country_code", "string", False),
        ("competition_type", DICTIONARY_STRING, False),
        ("source_name", DICTIONARY_STRING, False),
        ("source_competition_code", "string", False),
        ("timezone", "string", False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "seasons": [
        ("season_id", "string", False),
        ("competition_id", "string", False),
        ("label", "string", False),
        ("start_year", "int16", False),
        ("end_year", "int16", False),
        ("source_season_code", "string", False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "participants": [
        ("canonical_participant_id", "string", False),
        ("sport_code", DICTIONARY_STRING, False),
        ("competition_id", "string", False),
        ("participant_type", DICTIONARY_STRING, False),
        ("canonical_key", "string", False),
        ("display_name", "string", False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "source_participants": [
        ("source_participant_id", "string", False),
        ("source_name", DICTIONARY_STRING, False),
        ("source_participant_key", "string", False),
        ("competition_id", "string", False),
        ("canonical_participant_id", "string", True),
        ("participant_type", DICTIONARY_STRING, False),
        ("display_name", "string", False),
        ("normalized_name", "string", False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "participant_reconciliations": [
        ("source_name", DICTIONARY_STRING, False),
        ("source_participant_id", "string", False),
        ("source_participant_key", "string", False),
        ("canonical_participant_id", "string", True),
        ("reconciliation_state", DICTIONARY_STRING, False),
        ("reconciliation_confidence", "double", False),
        ("reconciliation_policy_version", DICTIONARY_STRING, False),
        ("match_key", "string", True),
        ("reason", "string", True),
        ("source_observed_at_utc", TIMESTAMP_UTC, False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "events": [
        ("canonical_event_id", "string", False),
        ("sport_code", DICTIONARY_STRING, False),
        ("competition_id", "string", False),
        ("season_id", "string", False),
        ("event_occurrence_key", "string", False),
        ("event_date", "date32[day]", False),
        ("scheduled_start_utc", TIMESTAMP_UTC, True),
        ("start_time_precision", DICTIONARY_STRING, False),
        ("status", DICTIONARY_STRING, False),
        ("home_canonical_participant_id", "string", False),
        ("away_canonical_participant_id", "string", False),
        ("home_score", "int16", True),
        ("away_score", "int16", True),
        ("result_code", DICTIONARY_STRING, True),
        ("outcome_availability_stage", DICTIONARY_STRING, False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "source_events": [
        ("source_name", DICTIONARY_STRING, False),
        ("source_event_id", "string", False),
        ("source_event_key", "string", False),
        ("canonical_event_id", "string", True),
        ("competition_id", "string", False),
        ("season_id", "string", False),
        ("event_occurrence_key", "string", True),
        ("event_date", "date32[day]", True),
        ("scheduled_start_utc", TIMESTAMP_UTC, True),
        ("start_time_precision", DICTIONARY_STRING, False),
        ("status", DICTIONARY_STRING, False),
        ("home_source_participant_id", "string", False),
        ("away_source_participant_id", "string", False),
        ("home_canonical_participant_id", "string", True),
        ("away_canonical_participant_id", "string", True),
        ("home_score", "int16", True),
        ("away_score", "int16", True),
        ("result_code", DICTIONARY_STRING, True),
        ("outcome_availability_stage", DICTIONARY_STRING, False),
        ("source_row_number", "int32", False),
        ("source_file_sha256", "string", False),
        ("source_observed_at_utc", TIMESTAMP_UTC, False),
        ("reconciliation_state", DICTIONARY_STRING, False),
        ("reconciliation_confidence", "double", False),
        ("reconciliation_policy_version", DICTIONARY_STRING, False),
        ("reconciliation_reason", "string", True),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "event_reconciliations": [
        ("source_name", DICTIONARY_STRING, False),
        ("source_event_id", "string", False),
        ("source_event_key", "string", False),
        ("canonical_event_id", "string", True),
        ("reconciliation_state", DICTIONARY_STRING, False),
        ("reconciliation_confidence", "double", False),
        ("reconciliation_policy_version", DICTIONARY_STRING, False),
        ("match_key", "string", True),
        ("reason", "string", True),
        ("source_observed_at_utc", TIMESTAMP_UTC, False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "market_quotes": [
        ("quote_series_id", "string", False),
        ("quote_observation_id", "string", False),
        ("canonical_event_id", "string", False),
        ("source_name", DICTIONARY_STRING, False),
        ("source_event_id", "string", False),
        ("sport_code", DICTIONARY_STRING, False),
        ("provider_type", DICTIONARY_STRING, False),
        ("provider_id", DICTIONARY_STRING, False),
        ("market_family", DICTIONARY_STRING, False),
        ("market_key", DICTIONARY_STRING, False),
        ("market_period", DICTIONARY_STRING, False),
        ("participant_scope", DICTIONARY_STRING, False),
        ("canonical_participant_id", "string", True),
        ("line_type", DICTIONARY_STRING, False),
        ("line_value", "decimal128(8, 2)", True),
        ("outcome_key", DICTIONARY_STRING, False),
        ("decimal_odds", "decimal128(10, 4)", False),
        ("quote_phase", DICTIONARY_STRING, False),
        ("source_observed_at_utc", TIMESTAMP_UTC, False),
        ("quoted_at_utc", TIMESTAMP_UTC, True),
        ("quote_timestamp_precision", DICTIONARY_STRING, False),
        ("quote_valid_from_utc", TIMESTAMP_UTC, True),
        ("quote_valid_to_utc", TIMESTAMP_UTC, True),
        ("market_status", DICTIONARY_STRING, False),
        ("selection_status", DICTIONARY_STRING, False),
        ("source_market_id", "string", True),
        ("source_selection_id", "string", True),
        ("source_field", "string", True),
        ("quality_status", DICTIONARY_STRING, False),
        ("quality_reason", "string", True),
        ("source_file_sha256", "string", False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
    "post_match_statistics": [
        ("canonical_event_id", "string", False),
        ("source_event_id", "string", False),
        ("availability_stage", DICTIONARY_STRING, False),
        ("half_time_home_goals", "int16", True),
        ("half_time_away_goals", "int16", True),
        ("half_time_result", DICTIONARY_STRING, True),
        ("referee", "string", True),
        ("home_shots", "int16", True),
        ("away_shots", "int16", True),
        ("home_shots_on_target", "int16", True),
        ("away_shots_on_target", "int16", True),
        ("home_corners", "int16", True),
        ("away_corners", "int16", True),
        ("home_fouls", "int16", True),
        ("away_fouls", "int16", True),
        ("home_yellow_cards", "int16", True),
        ("away_yellow_cards", "int16", True),
        ("home_red_cards", "int16", True),
        ("away_red_cards", "int16", True),
        ("source_observed_at_utc", TIMESTAMP_UTC, False),
        ("source_file_sha256", "string", False),
        ("schema_version", DICTIONARY_STRING, False),
    ],
}

EXPECTED_NULLABLE_FIELDS: dict[str, set[str]] = {
    "competitions": set(),
    "seasons": set(),
    "participants": set(),
    "source_participants": {"canonical_participant_id"},
    "participant_reconciliations": {
        "canonical_participant_id",
        "match_key",
        "reason",
    },
    "events": {
        "scheduled_start_utc",
        "home_score",
        "away_score",
        "result_code",
    },
    "source_events": {
        "canonical_event_id",
        "event_occurrence_key",
        "event_date",
        "scheduled_start_utc",
        "home_canonical_participant_id",
        "away_canonical_participant_id",
        "home_score",
        "away_score",
        "result_code",
        "reconciliation_reason",
    },
    "event_reconciliations": {
        "canonical_event_id",
        "match_key",
        "reason",
    },
    "market_quotes": {
        "canonical_participant_id",
        "line_value",
        "quoted_at_utc",
        "quote_valid_from_utc",
        "quote_valid_to_utc",
        "source_market_id",
        "source_selection_id",
        "source_field",
        "quality_reason",
    },
    "post_match_statistics": {
        "half_time_home_goals",
        "half_time_away_goals",
        "half_time_result",
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

EXPECTED_FINGERPRINTS = {
    "competitions": "176b0c72d1e9540ac39bf3b6f784c7ceb87fb01b61b0274968eb636e1d18c43d",
    "seasons": "405d7ae31d3ac696a1665a953d03c5d90d5c2964ecb0807626349f4147279c37",
    "participants": ("1a1c9b70f21d0fc504a7d0023711b00b4f89462e5dd27f0579d74d53178e15e9"),
    "source_participants": ("45f11469fe57b11d57fb1fa86dca839816ab8c13a201610966d2d1f69d0a1103"),
    "participant_reconciliations": (
        "8dbcd38f873aa54aa72a882832d578f7d286f73bc6a2a606752f48fe7dc16590"
    ),
    "events": "927b0e1848798b84d39a243432a7f70bc774582c08db66b1274e00ba31addcb4",
    "source_events": ("58a1d119e0b829f86ed77da2f9a94dae52ecfeccfa3c2f39e51d611f42fe922c"),
    "event_reconciliations": "526bdd5ea0977488f28d201b15043378d2a6b4f8354fdc51b2182d71ebe40d7b",
    "market_quotes": "42941e0f8cdc107bbed850f42a7ac510a19b93b892844f49d66a84304120c871",
    "post_match_statistics": "ac215e601f6ba9af7ae5b36459dfd315b86865b48337ccd4a8c29a9527a037bc",
}

EXPECTED_ROW_COUNTS_WITH_ODDS = {
    "competitions": 1,
    "seasons": 1,
    "participants": 2,
    "source_participants": 2,
    "participant_reconciliations": 2,
    "events": 1,
    "source_events": 1,
    "event_reconciliations": 1,
    "market_quotes": 3,
    "post_match_statistics": 1,
}


def _field_table(schema: pa.Schema) -> list[tuple[str, str, bool]]:
    return [(field.name, str(field.type), field.nullable) for field in schema]


def _tables(tmp_path: Path, *, content: bytes) -> dict[str, pa.Table]:
    _spec, bundle = build_spec(tmp_path, content=content)
    return build_tables(bundle)


def test_football_suite_exposes_the_expected_datasets_in_order() -> None:
    suite = football_snapshot_suite()

    assert suite.dataset_names == EXPECTED_DATASETS
    assert CANONICAL_DATASETS == EXPECTED_DATASETS
    assert suite.primary_dataset_name == "events"
    assert [descriptor.relative_filename for descriptor in suite.descriptors] == [
        f"{dataset_name}.parquet" for dataset_name in EXPECTED_DATASETS
    ]
    assert PARQUET_FILENAMES == {
        dataset_name: f"{dataset_name}.parquet" for dataset_name in EXPECTED_DATASETS
    }
    assert suite.expected_directory_files == frozenset(
        {f"{dataset_name}.parquet" for dataset_name in EXPECTED_DATASETS} | {"manifest.json"}
    )


@pytest.mark.parametrize("dataset_name", EXPECTED_DATASETS)
def test_dataset_schema_fields_match_the_documented_contract(dataset_name: str) -> None:
    assert _field_table(dataset_schema(dataset_name)) == EXPECTED_FIELDS[dataset_name]


def test_market_quote_decimal_types_use_the_shared_precision_policy() -> None:
    schema = dataset_schema("market_quotes")

    assert schema.field("decimal_odds").type == pa.decimal128(
        PRICE_DECIMAL_PRECISION,
        PRICE_DECIMAL_SCALE,
    )
    assert schema.field("decimal_odds").type == pa.decimal128(10, 4)
    assert schema.field("line_value").type == pa.decimal128(
        LINE_DECIMAL_PRECISION,
        LINE_DECIMAL_SCALE,
    )
    assert schema.field("line_value").type == pa.decimal128(8, 2)


@pytest.mark.parametrize("dataset_name", EXPECTED_DATASETS)
def test_only_documented_fields_are_nullable(dataset_name: str) -> None:
    schema = dataset_schema(dataset_name)

    assert {field.name for field in schema if field.nullable} == EXPECTED_NULLABLE_FIELDS[
        dataset_name
    ]


def test_schema_fingerprint_changes_when_nullability_flips() -> None:
    schema = dataset_schema("events")
    index = schema.get_field_index("competition_id")
    flipped = schema.set(index, schema.field(index).with_nullable(True))

    assert _field_table(flipped) != _field_table(schema)
    assert flipped.metadata == schema.metadata
    assert schema_fingerprint(flipped) != schema_fingerprint(schema)


def test_football_schema_fingerprints_match_the_documented_values() -> None:
    fingerprints = football_schema_fingerprints()

    assert fingerprints == EXPECTED_FINGERPRINTS
    assert {
        dataset_name: schema_fingerprint(dataset_schema(dataset_name))
        for dataset_name in EXPECTED_DATASETS
    } == EXPECTED_FINGERPRINTS


def test_bundle_to_tables_matches_descriptor_schemas_and_row_counts(tmp_path: Path) -> None:
    _spec, bundle = build_spec(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)

    tables = bundle_to_tables(bundle)

    assert tuple(tables) == EXPECTED_DATASETS
    assert {name: table.num_rows for name, table in tables.items()} == (
        EXPECTED_ROW_COUNTS_WITH_ODDS
    )
    for descriptor in football_snapshot_suite().descriptors:
        assert tables[descriptor.dataset_name].schema == descriptor.schema


def test_zero_row_optional_tables_retain_the_full_schema(tmp_path: Path) -> None:
    tables = _tables(tmp_path, content=SYNTHETIC_CSV)

    assert tables["market_quotes"].num_rows == 0
    assert tables["post_match_statistics"].num_rows == 0
    assert tables["market_quotes"].schema == dataset_schema("market_quotes")
    assert _field_table(tables["market_quotes"].schema) == EXPECTED_FIELDS["market_quotes"]
    assert (
        _field_table(tables["post_match_statistics"].schema)
        == (EXPECTED_FIELDS["post_match_statistics"])
    )


def test_written_parquet_files_read_back_with_identical_schemas(tmp_path: Path) -> None:
    suite = football_snapshot_suite()
    tables = _tables(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)
    directory = tmp_path / "snapshot"
    directory.mkdir()

    file_meta = write_suite_parquet_files(directory, suite=suite, tables=tables)

    assert set(file_meta) == set(EXPECTED_DATASETS)
    assert {path.name for path in directory.iterdir()} == set(suite.filenames)
    for descriptor in suite.descriptors:
        read_back = pq.read_table(directory / descriptor.relative_filename)
        assert read_back.schema == descriptor.schema
        assert _field_table(read_back.schema) == EXPECTED_FIELDS[descriptor.dataset_name]
        assert read_back.num_rows == EXPECTED_ROW_COUNTS_WITH_ODDS[descriptor.dataset_name]
        assert (
            file_meta[descriptor.dataset_name]["schema_fingerprint"]
            == (EXPECTED_FINGERPRINTS[descriptor.dataset_name])
        )


def test_required_field_rejects_a_null_value(tmp_path: Path) -> None:
    schema = dataset_schema("events")
    tables = _tables(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)
    row = dict(tables["events"].to_pylist()[0])
    row["competition_id"] = None

    with pytest.raises(pa.ArrowInvalid, match="non-nullable"):
        write_parquet_file(
            tmp_path / "events.parquet",
            pa.Table.from_pylist([row], schema=schema),
        )


def test_verify_parquet_file_rejects_a_row_count_mismatch(tmp_path: Path) -> None:
    suite = football_snapshot_suite()
    directory = tmp_path / "snapshot"
    directory.mkdir()
    write_suite_parquet_files(
        directory,
        suite=suite,
        tables=_tables(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS),
    )

    with pytest.raises(SnapshotIntegrityError, match="row count mismatch"):
        verify_parquet_file(
            directory / "events.parquet",
            expected_schema=dataset_schema("events"),
            expected_rows=2,
        )


def test_verify_parquet_file_rejects_pandas_metadata(tmp_path: Path) -> None:
    path = tmp_path / "events.parquet"
    schema = dataset_schema("events").with_metadata({b"pandas": b"{}"})
    pq.write_table(pa.Table.from_pylist([], schema=schema), path)

    with pytest.raises(SnapshotIntegrityError, match="pandas metadata"):
        verify_parquet_file(
            path,
            expected_schema=dataset_schema("events"),
            expected_rows=0,
        )


@pytest.mark.parametrize(
    "source_path",
    [
        pytest.param(b"/var/data/source.csv", id="posix"),
        pytest.param(b"C:\\data\\source.csv", id="windows_drive"),
        pytest.param(b"\\\\server\\share\\file.csv", id="unc"),
    ],
)
def test_verify_parquet_file_rejects_absolute_path_metadata(
    tmp_path: Path,
    source_path: bytes,
) -> None:
    path = tmp_path / "events.parquet"
    schema = dataset_schema("events").with_metadata({b"source_path": source_path})
    pq.write_table(pa.Table.from_pylist([], schema=schema), path)

    with pytest.raises(SnapshotIntegrityError, match="absolute path metadata"):
        verify_parquet_file(
            path,
            expected_schema=dataset_schema("events"),
            expected_rows=0,
        )


def test_write_suite_parquet_files_rejects_a_missing_dataset_table(tmp_path: Path) -> None:
    tables = _tables(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)
    del tables["market_quotes"]
    directory = tmp_path / "snapshot"
    directory.mkdir()

    with pytest.raises(SnapshotIntegrityError, match=r"missing=\['market_quotes'\]"):
        write_suite_parquet_files(directory, suite=football_snapshot_suite(), tables=tables)


def test_write_suite_parquet_files_rejects_an_unexpected_dataset_table(tmp_path: Path) -> None:
    tables = _tables(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)
    tables["injuries"] = tables["events"]
    directory = tmp_path / "snapshot"
    directory.mkdir()

    with pytest.raises(SnapshotIntegrityError, match=r"unexpected=\['injuries'\]"):
        write_suite_parquet_files(directory, suite=football_snapshot_suite(), tables=tables)


def test_write_suite_parquet_files_rejects_unexpected_preexisting_files(tmp_path: Path) -> None:
    tables = _tables(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)
    directory = tmp_path / "snapshot"
    directory.mkdir()
    (directory / "leftover.txt").write_text("not part of a snapshot", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="unexpected files present"):
        write_suite_parquet_files(directory, suite=football_snapshot_suite(), tables=tables)

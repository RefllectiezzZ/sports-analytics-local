"""Section 56: canonical football snapshot manifest tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.snapshots import manifest, parquet
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv
from sports_analytics.sports.football.contracts import (
    CANONICAL_DATASETS,
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    MANIFEST_VERSION,
    PARQUET_FILENAMES,
)
from sports_analytics.sports.football.identifiers import build_season_id
from sports_analytics.sports.football.normalization import normalize_football_rows
from sports_analytics.sports.football.schemas import dataset_schema, schema_fingerprint

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
SYNTHETIC_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)


def _bundle():
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


def _manifest_document(tmp_path: Path) -> dict[str, object]:
    bundle = _bundle()
    file_meta = parquet.write_bundle_parquet_files(tmp_path, bundle)
    raw_sha = hashlib.sha256(SYNTHETIC_CSV).hexdigest()
    return manifest.build_manifest_document(
        snapshot_id="11111111-1111-4111-8111-111111111111",
        source_name="football-data-co-uk",
        source_version="football-data-co-uk:e0:2324:sha256:" + raw_sha,
        source_competition_code="E0",
        source_season_code="2324",
        competition_id="eng-premier-league",
        season_id=build_season_id(
            competition_id="eng-premier-league",
            label="2023-2024",
        ),
        source_url="https://www.football-data.co.uk/mmz4281/2324/E0.csv",
        source_observed_at_utc=OBSERVED_AT,
        raw_relative_path=f"football-data-co-uk/sha256/{raw_sha[:2]}/{raw_sha}.csv",
        raw_checksum_sha256=raw_sha,
        raw_bytes=len(SYNTHETIC_CSV),
        raw_encoding="utf-8",
        http_status=200,
        http_content_type="text/csv",
        http_content_length=len(SYNTHETIC_CSV),
        http_etag='"etag-1"',
        http_last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        http_final_url="https://www.football-data.co.uk/mmz4281/2324/E0.csv",
        bundle=bundle,
        file_meta=file_meta,
        snapshot_relative_directory=(
            "football-ingestion/football-canonical-v1/eng-premier-league/"
            "2023-2024/11111111-1111-4111-8111-111111111111"
        ),
        unknown_source_columns=("Zed", "Alpha"),
        missing_optional_source_columns=("Time", "HTR"),
    )


def test_build_manifest_document_contains_identity_schema_and_quality(
    tmp_path: Path,
) -> None:
    document = _manifest_document(tmp_path)

    assert document["manifest_version"] == MANIFEST_VERSION
    assert document["snapshot_id"] == "11111111-1111-4111-8111-111111111111"
    assert document["schema_version"] == FOOTBALL_CANONICAL_SCHEMA_VERSION
    assert document["source_name"] == "football-data-co-uk"
    assert document["source_observed_at_utc"] == "2024-01-15T12:00:00.000000Z"
    assert document["unknown_source_columns"] == ["Alpha", "Zed"]
    assert document["missing_optional_source_columns"] == ["HTR", "Time"]
    assert document["quality_summary"] == {
        "duplicate_rows_discarded": 0,
        "pinnacle_caution_quote_count": 0,
        "warnings_count": 0,
    }
    assert document["http_metadata"] == {
        "content_length": len(SYNTHETIC_CSV),
        "content_type": "text/csv",
        "etag": '"etag-1"',
        "final_url": "https://www.football-data.co.uk/mmz4281/2324/E0.csv",
        "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        "status": 200,
    }
    assert document["row_counts"] == {
        "competitions": 1,
        "seasons": 1,
        "teams": 2,
        "games": 1,
        "odds_1x2": 0,
        "post_match_statistics": 0,
    }
    assert document["schema_fingerprints"] == {
        dataset: schema_fingerprint(dataset_schema(dataset)) for dataset in CANONICAL_DATASETS
    }


def test_manifest_files_are_ordered_by_canonical_dataset_order(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path)
    files = document["files"]

    assert isinstance(files, list)
    assert [item["relative_filename"] for item in files if isinstance(item, dict)] == [
        PARQUET_FILENAMES[dataset] for dataset in CANONICAL_DATASETS
    ]


def test_manifest_serialization_is_canonical_and_loadable(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path)
    path = tmp_path / "manifest.json"

    payload, digest = manifest.write_manifest(path, document)
    loaded, loaded_payload, loaded_digest = manifest.load_manifest_bytes(path)

    assert payload.endswith(b"\n")
    assert payload == manifest.serialize_manifest(document)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert loaded.document == document
    assert loaded.snapshot_id == document["snapshot_id"]
    assert loaded.games_count == 1
    assert loaded.teams_count == 2
    assert loaded_payload == payload
    assert loaded_digest == digest


def test_load_manifest_bytes_rejects_missing_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    payload = manifest.serialize_manifest(_manifest_document(tmp_path / "data"))
    path.write_bytes(payload.rstrip(b"\n"))

    with pytest.raises(SnapshotVerificationError, match="end with a newline"):
        manifest.load_manifest_bytes(path)


def test_load_manifest_bytes_rejects_unsupported_manifest_version(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path / "data")
    document["manifest_version"] = "future-version"
    path = tmp_path / "manifest.json"
    path.write_bytes(manifest.serialize_manifest(document))

    with pytest.raises(SnapshotVerificationError, match="unsupported manifest_version"):
        manifest.load_manifest_bytes(path)


def test_validate_manifest_document_rejects_bool_integer_fields(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path / "data")
    document["raw_artifact_bytes"] = True

    with pytest.raises(SnapshotVerificationError, match="raw_artifact_bytes"):
        manifest.validate_manifest_document(document)


def test_validate_manifest_document_rejects_duplicate_file_entries(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path / "data")
    files = document["files"]
    assert isinstance(files, list)
    files[1] = dict(files[0])

    with pytest.raises(SnapshotVerificationError, match="duplicate entry"):
        manifest.validate_manifest_document(document)


def test_validate_manifest_document_rejects_schema_fingerprint_mismatch(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path / "data")
    schema_fingerprints = document["schema_fingerprints"]
    assert isinstance(schema_fingerprints, dict)
    schema_fingerprints["games"] = "0" * 64

    with pytest.raises(SnapshotVerificationError, match="schema fingerprint mismatch"):
        manifest.validate_manifest_document(document)


def test_expected_parquet_filenames_matches_contract() -> None:
    assert manifest.expected_parquet_filenames() == frozenset(PARQUET_FILENAMES.values())


def test_validate_manifest_identity_accepts_matching_core_fields(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path)
    manifest.validate_manifest_identity(
        document,
        snapshot_id=str(document["snapshot_id"]),
        source_version=str(document["source_version"]),
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        competition_id="eng-premier-league",
        season_id=str(document["season_id"]),
    )


def test_validate_manifest_identity_rejects_mismatched_core_fields(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path)

    with pytest.raises(SnapshotIntegrityError, match="source_version"):
        manifest.validate_manifest_identity(
            document,
            snapshot_id=str(document["snapshot_id"]),
            source_version="different-source-version",
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
            competition_id="eng-premier-league",
            season_id=str(document["season_id"]),
        )

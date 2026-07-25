"""Snapshot manifest construction, HTTP metadata, and hostile-input validation tests.

Every manifest here is built from synthetic CSV bytes through the sport-agnostic
snapshot contracts, so the tests never touch the network and never depend on a
sport-specific manifest builder.
"""

from __future__ import annotations

import copy
import getpass
import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.snapshots.manifest import (
    build_manifest_document,
    load_manifest_bytes,
    serialize_manifest,
    validate_manifest_document,
    validate_manifest_identity,
    write_manifest,
)
from sports_analytics.snapshots.parquet import write_suite_parquet_files
from sports_analytics.snapshots.spec import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    SnapshotHttpMetadata,
    SnapshotSpec,
)
from sports_analytics.sports.football.schemas import (
    football_schema_fingerprints,
    football_snapshot_suite,
)
from tests.helpers_snapshots import SYNTHETIC_CSV_WITH_ODDS, build_spec, build_tables

SNAPSHOT_ID = "11111111-1111-4111-8111-111111111111"

EXPECTED_TOP_LEVEL_KEYS = frozenset(
    {
        "manifest_version",
        "snapshot_id",
        "snapshot_type",
        "schema_version",
        "source_name",
        "source_version",
        "source_policy_version",
        "source_url",
        "source_observed_at_utc",
        "partition_keys",
        "domain_metadata",
        "producer_versions",
        "raw_artifact",
        "http_metadata",
        "python_version",
        "pyarrow_version",
        "schema_fingerprints",
        "files",
        "row_counts",
        "quality_summary",
        "warnings",
        "generated_snapshot_relative_path",
    }
)

EXPECTED_ROW_COUNTS = {
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

CREDENTIAL_MARKERS = ("password", "secret", "token", "api_key", "authorization")


def _live_http_metadata(source_url: str) -> SnapshotHttpMetadata:
    """Return complete HTTP metadata for a snapshot retrieved over the network."""
    return SnapshotHttpMetadata(
        network_retrieved=True,
        status=200,
        content_type="text/csv",
        content_length=len(SYNTHETIC_CSV_WITH_ODDS),
        etag='"etag-1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        final_url=source_url,
    )


def _build(
    tmp_path: Path, *, network_retrieved: bool = False
) -> tuple[SnapshotSpec, dict[str, Any]]:
    """Build a snapshot spec and its manifest document from synthetic CSV bytes."""
    spec, bundle = build_spec(tmp_path, content=SYNTHETIC_CSV_WITH_ODDS)
    if network_retrieved:
        spec = replace(spec, http_metadata=_live_http_metadata(spec.source_url))
    directory = tmp_path / "snapshot"
    directory.mkdir(parents=True)
    file_meta = write_suite_parquet_files(
        directory,
        suite=spec.suite,
        tables=build_tables(bundle),
    )
    document = build_manifest_document(
        snapshot_id=SNAPSHOT_ID,
        spec=spec,
        file_meta=file_meta,
        snapshot_relative_directory=spec.identity.relative_directory(SNAPSHOT_ID),
    )
    return spec, document


def _document(tmp_path: Path, *, network_retrieved: bool = False) -> dict[str, Any]:
    """Return only the manifest document for a synthetic snapshot."""
    return _build(tmp_path, network_retrieved=network_retrieved)[1]


@pytest.fixture(scope="module")
def cached_document(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Return one valid cached-acquisition manifest document shared by read-only tests."""
    return _document(tmp_path_factory.mktemp("cached"))


def _files(document: dict[str, Any]) -> list[dict[str, Any]]:
    files = document["files"]
    assert isinstance(files, list)
    return files


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for entry in value for item in _strings(entry)]
    if isinstance(value, dict):
        return [item for key, entry in value.items() for item in _strings(key) + _strings(entry)]
    return []


def test_manifest_document_contains_every_required_top_level_key(
    cached_document: dict[str, Any],
) -> None:
    assert frozenset(cached_document) == EXPECTED_TOP_LEVEL_KEYS
    assert cached_document["manifest_version"] == MANIFEST_VERSION
    assert cached_document["manifest_version"] == "snapshot-manifest-v2"
    assert cached_document["snapshot_id"] == SNAPSHOT_ID
    assert cached_document["snapshot_type"] == "football-ingestion"
    assert cached_document["source_name"] == "football-data-co-uk"
    assert cached_document["source_observed_at_utc"] == "2024-01-15T12:00:00.000000Z"
    assert cached_document["generated_snapshot_relative_path"].endswith(SNAPSHOT_ID)


def test_manifest_document_records_partition_keys_as_a_mapping(
    cached_document: dict[str, Any],
) -> None:
    assert cached_document["partition_keys"] == {
        "competition_id": "eng-premier-league",
        "season_label": "2023-2024",
    }


def test_manifest_document_records_every_producer_version(
    cached_document: dict[str, Any],
) -> None:
    producer_versions = cached_document["producer_versions"]

    assert set(producer_versions) == {
        "adapter",
        "parser",
        "normalizer",
        "event_reconciliation",
        "participant_reconciliation",
    }
    assert all(isinstance(value, str) and value for value in producer_versions.values())


def test_manifest_document_records_the_raw_artifact_reference(
    cached_document: dict[str, Any],
) -> None:
    raw_artifact = cached_document["raw_artifact"]

    assert set(raw_artifact) == {"relative_path", "checksum_sha256", "byte_count", "encoding"}
    assert raw_artifact["checksum_sha256"] == hashlib.sha256(SYNTHETIC_CSV_WITH_ODDS).hexdigest()
    assert raw_artifact["byte_count"] == len(SYNTHETIC_CSV_WITH_ODDS)
    assert raw_artifact["encoding"] == "utf-8"
    assert not raw_artifact["relative_path"].startswith("/")


def test_manifest_document_row_counts_match_the_file_entries(
    cached_document: dict[str, Any],
) -> None:
    suite = football_snapshot_suite()
    files = _files(cached_document)

    assert cached_document["row_counts"] == EXPECTED_ROW_COUNTS
    assert [entry["relative_filename"] for entry in files] == [
        f"{dataset_name}.parquet" for dataset_name in suite.dataset_names
    ]
    assert {
        dataset_name: files[index]["row_count"]
        for index, dataset_name in enumerate(suite.dataset_names)
    } == EXPECTED_ROW_COUNTS


def test_manifest_document_schema_fingerprints_match_the_football_suite(
    cached_document: dict[str, Any],
) -> None:
    expected = football_schema_fingerprints()

    assert cached_document["schema_fingerprints"] == expected
    assert {entry["schema_fingerprint"] for entry in _files(cached_document)} == set(
        expected.values()
    )


def test_manifest_document_excludes_absolute_paths_usernames_and_credentials(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    payload = serialize_manifest(document).decode("utf-8")

    assert str(tmp_path) not in payload
    assert str(Path.home()) not in payload
    assert getpass.getuser() not in payload
    for marker in CREDENTIAL_MARKERS:
        assert marker not in payload.lower()
    for value in _strings(document):
        assert not value.startswith("/")
        assert "\\" not in value
        assert not (len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"})


def test_manifest_serialization_is_deterministic_with_a_trailing_newline(
    tmp_path: Path,
) -> None:
    first = serialize_manifest(_document(tmp_path / "first"))
    second = serialize_manifest(_document(tmp_path / "second"))

    assert first.endswith(b"\n")
    assert first.count(b"\n") == 1
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_write_and_load_manifest_round_trips_canonical_json(tmp_path: Path) -> None:
    document = _document(tmp_path)
    path = tmp_path / MANIFEST_FILENAME

    payload, digest = write_manifest(path, document)
    manifest, loaded_payload, loaded_digest = load_manifest_bytes(
        path,
        suite=football_snapshot_suite(),
    )

    assert payload == serialize_manifest(document)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert loaded_payload == payload
    assert loaded_digest == digest
    assert manifest.document == document
    assert manifest.snapshot_id == SNAPSHOT_ID
    assert manifest.row_counts == EXPECTED_ROW_COUNTS


def test_load_manifest_bytes_rejects_a_missing_final_newline(tmp_path: Path) -> None:
    document = _document(tmp_path)
    path = tmp_path / MANIFEST_FILENAME
    path.write_bytes(serialize_manifest(document).rstrip(b"\n"))

    with pytest.raises(SnapshotVerificationError, match="end with a newline"):
        load_manifest_bytes(path, suite=football_snapshot_suite())


def test_manifest_records_complete_http_metadata_for_a_live_retrieval(tmp_path: Path) -> None:
    spec, document = _build(tmp_path, network_retrieved=True)

    assert document["http_metadata"] == {
        "network_retrieved": True,
        "status": 200,
        "content_type": "text/csv",
        "content_length": len(SYNTHETIC_CSV_WITH_ODDS),
        "etag": '"etag-1"',
        "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        "final_url": spec.source_url,
    }
    assert document["source_url"] == spec.source_url

    manifest = validate_manifest_document(document, suite=football_snapshot_suite())

    assert manifest.http_metadata.network_retrieved is True
    assert manifest.http_metadata.status == 200
    assert manifest.source_url == spec.source_url


def test_manifest_records_null_http_metadata_for_a_cached_retrieval(tmp_path: Path) -> None:
    spec, document = _build(tmp_path)

    assert document["http_metadata"] == {
        "network_retrieved": False,
        "status": None,
        "content_type": None,
        "content_length": None,
        "etag": None,
        "last_modified": None,
        "final_url": None,
    }
    assert document["source_url"] == spec.source_url

    manifest = validate_manifest_document(document, suite=football_snapshot_suite())

    assert manifest.http_metadata.network_retrieved is False
    assert manifest.http_metadata.status is None
    assert manifest.source_url == spec.source_url


def test_validate_manifest_document_accepts_a_freshly_built_document(
    cached_document: dict[str, Any],
) -> None:
    manifest = validate_manifest_document(cached_document, suite=football_snapshot_suite())

    assert manifest.manifest_version == MANIFEST_VERSION
    assert manifest.partition_keys == (
        ("competition_id", "eng-premier-league"),
        ("season_label", "2023-2024"),
    )
    assert manifest.schema_fingerprints == football_schema_fingerprints()
    assert tuple(item.dataset_name for item in manifest.files) == (
        football_snapshot_suite().dataset_names
    )


def _drop_snapshot_id(document: dict[str, Any]) -> None:
    del document["snapshot_id"]


def _non_string_snapshot_id(document: dict[str, Any]) -> None:
    document["snapshot_id"] = 11111111


def _non_canonical_snapshot_id(document: dict[str, Any]) -> None:
    document["snapshot_id"] = SNAPSHOT_ID.replace("-", "")


def _malformed_snapshot_id(document: dict[str, Any]) -> None:
    document["snapshot_id"] = "not-a-uuid"


def _unsupported_manifest_version(document: dict[str, Any]) -> None:
    document["manifest_version"] = "snapshot-manifest-v99"


def _missing_row_count(document: dict[str, Any]) -> None:
    del document["row_counts"]["events"]


def _negative_row_count(document: dict[str, Any]) -> None:
    document["row_counts"]["events"] = -1


def _bool_row_count(document: dict[str, Any]) -> None:
    _files(document)[4]["row_count"] = True


def _enormous_row_count(document: dict[str, Any]) -> None:
    document["row_counts"]["events"] = 2**63


def _inconsistent_row_count(document: dict[str, Any]) -> None:
    document["row_counts"]["events"] = 2


def _duplicate_file_entry(document: dict[str, Any]) -> None:
    files = _files(document)
    files[1] = dict(files[0])


def _extra_file_entry(document: dict[str, Any]) -> None:
    files = _files(document)
    files.append(dict(files[0]))


def _missing_file_entry(document: dict[str, Any]) -> None:
    _files(document).pop()


def _wrong_filename(document: dict[str, Any]) -> None:
    _files(document)[0]["relative_filename"] = "unexpected.parquet"


def _wrong_suite_fingerprint(document: dict[str, Any]) -> None:
    document["schema_fingerprints"]["events"] = "0" * 64


def _wrong_file_fingerprint(document: dict[str, Any]) -> None:
    _files(document)[4]["schema_fingerprint"] = "0" * 64


def _wrong_source_name(document: dict[str, Any]) -> None:
    document["source_name"] = "Football-Data.Co.UK"


def _malformed_timestamp(document: dict[str, Any]) -> None:
    document["source_observed_at_utc"] = "2024-01-15 12:00:00"


def _extra_file_entry_key(document: dict[str, Any]) -> None:
    _files(document)[0]["unexpected"] = "value"


def _malformed_http_metadata(document: dict[str, Any]) -> None:
    document["http_metadata"]["status"] = 200


def _partition_value_with_colon(document: dict[str, Any]) -> None:
    document["partition_keys"]["season_label"] = "2023:2024"


def _partition_value_with_uppercase(document: dict[str, Any]) -> None:
    document["partition_keys"]["competition_id"] = "ENG-Premier-League"


def _nested_domain_metadata(document: dict[str, Any]) -> None:
    document["domain_metadata"]["sport_code"] = {"nested": "football"}


def _string_quality_summary_value(document: dict[str, Any]) -> None:
    document["quality_summary"]["warnings_count"] = "0"


HOSTILE_CASES: tuple[tuple[str, Callable[[dict[str, Any]], None], str], ...] = (
    ("missing_snapshot_id", _drop_snapshot_id, "missing required keys"),
    ("non_string_snapshot_id", _non_string_snapshot_id, "snapshot_id must be a string"),
    ("non_canonical_snapshot_id", _non_canonical_snapshot_id, "canonical lowercase UUID"),
    ("malformed_snapshot_id", _malformed_snapshot_id, "manifest validation failed"),
    ("unsupported_manifest_version", _unsupported_manifest_version, "unsupported manifest_version"),
    ("missing_row_count", _missing_row_count, "row_counts keys mismatch"),
    ("negative_row_count", _negative_row_count, "row_counts.events must be between"),
    ("bool_row_count", _bool_row_count, r"files\[4\].row_count must be an integer"),
    ("enormous_row_count", _enormous_row_count, "row_counts.events must be between"),
    ("inconsistent_row_count", _inconsistent_row_count, "row_counts mismatch for events"),
    ("duplicate_file_entry", _duplicate_file_entry, "duplicate entry"),
    ("extra_file_entry", _extra_file_entry, "exactly one entry per expected dataset"),
    ("missing_file_entry", _missing_file_entry, "exactly one entry per expected dataset"),
    ("wrong_filename", _wrong_filename, "unexpected filename"),
    ("wrong_suite_fingerprint", _wrong_suite_fingerprint, "schema fingerprint mismatch for events"),
    ("wrong_file_fingerprint", _wrong_file_fingerprint, "file schema fingerprint mismatch"),
    ("wrong_source_name", _wrong_source_name, "source_name is not a valid identifier"),
    ("malformed_timestamp", _malformed_timestamp, "canonical UTC timestamp"),
    ("extra_file_entry_key", _extra_file_entry_key, r"files\[0\] keys mismatch"),
    ("malformed_http_metadata", _malformed_http_metadata, "without a network request"),
    ("partition_value_with_colon", _partition_value_with_colon, "path-safe token"),
    ("partition_value_with_uppercase", _partition_value_with_uppercase, "path-safe token"),
    ("nested_domain_metadata", _nested_domain_metadata, "domain_metadata.sport_code must be"),
    ("string_quality_summary", _string_quality_summary_value, "quality_summary.warnings_count"),
)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [pytest.param(mutate, match, id=case_id) for case_id, mutate, match in HOSTILE_CASES],
)
def test_validate_manifest_document_rejects_hostile_documents(
    cached_document: dict[str, Any],
    mutate: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    document = copy.deepcopy(cached_document)
    mutate(document)

    with pytest.raises(SnapshotVerificationError, match=match):
        validate_manifest_document(document, suite=football_snapshot_suite())


def test_validate_manifest_document_rejects_a_non_object_document() -> None:
    with pytest.raises(SnapshotVerificationError, match="must be an object"):
        validate_manifest_document(["not", "a", "manifest"], suite=football_snapshot_suite())


def test_validate_manifest_identity_accepts_a_matching_identity(
    tmp_path: Path,
) -> None:
    spec, document = _build(tmp_path)
    manifest = validate_manifest_document(document, suite=football_snapshot_suite())

    validate_manifest_identity(
        manifest,
        snapshot_id=SNAPSHOT_ID,
        snapshot_type=spec.identity.snapshot_type,
        schema_version=spec.identity.schema_version,
        source_name=spec.identity.source_name,
        source_version=spec.identity.source_version,
        partition_keys=spec.identity.partition_keys,
    )


def test_validate_manifest_identity_rejects_a_mismatched_partition_key(tmp_path: Path) -> None:
    spec, document = _build(tmp_path)
    manifest = validate_manifest_document(document, suite=football_snapshot_suite())

    with pytest.raises(SnapshotIntegrityError, match="identity mismatch for partition_keys"):
        validate_manifest_identity(
            manifest,
            snapshot_id=SNAPSHOT_ID,
            snapshot_type=spec.identity.snapshot_type,
            schema_version=spec.identity.schema_version,
            source_name=spec.identity.source_name,
            source_version=spec.identity.source_version,
            partition_keys=(
                ("competition_id", "eng-premier-league"),
                ("season_label", "2024-2025"),
            ),
        )


def test_validate_manifest_identity_rejects_a_mismatched_source_version(tmp_path: Path) -> None:
    spec, document = _build(tmp_path)
    manifest = validate_manifest_document(document, suite=football_snapshot_suite())

    with pytest.raises(SnapshotIntegrityError, match="identity mismatch for source_version"):
        validate_manifest_identity(
            manifest,
            snapshot_id=SNAPSHOT_ID,
            snapshot_type=spec.identity.snapshot_type,
            schema_version=spec.identity.schema_version,
            source_name=spec.identity.source_name,
            source_version="e0:2324:sha256:" + "0" * 64,
            partition_keys=spec.identity.partition_keys,
        )

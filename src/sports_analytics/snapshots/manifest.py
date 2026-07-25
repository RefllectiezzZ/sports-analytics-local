"""Canonical snapshot manifest construction and serialization."""

from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

import pyarrow as pa

from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    format_utc_timestamp,
    loads_canonical_json,
    parse_utc_timestamp,
)
from sports_analytics.data.types import (
    JsonValue,
    normalize_uuid,
    validate_identifier,
    validate_relative_snapshot_path,
    validate_sha256_checksum,
)
from sports_analytics.sports.football.contracts import (
    CANONICAL_DATASETS,
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
    FOOTBALL_NORMALIZER_VERSION,
    FOOTBALL_PARSER_VERSION,
    MANIFEST_VERSION,
    PARQUET_FILENAMES,
)
from sports_analytics.sports.football.normalization import NormalizedFootballBundle
from sports_analytics.sports.football.schemas import dataset_schema, schema_fingerprint

MAX_MANIFEST_INT: Final[int] = 2**63 - 1
_HTTP_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "status",
        "content_type",
        "content_length",
        "etag",
        "last_modified",
        "final_url",
    }
)
_QUALITY_SUMMARY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "pinnacle_caution_quote_count",
        "duplicate_rows_discarded",
        "warnings_count",
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedManifestFile:
    """Typed metadata for one canonical Parquet file entry in a manifest."""

    dataset_name: str
    relative_filename: str
    sha256: str
    byte_count: int
    row_count: int
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class ValidatedQualitySummary:
    """Typed quality counters from a validated manifest."""

    pinnacle_caution_quote_count: int
    duplicate_rows_discarded: int
    warnings_count: int


@dataclass(frozen=True, slots=True)
class ValidatedManifest:
    """A manifest document validated for read-only verification/publication."""

    document: dict[str, JsonValue]
    snapshot_id: str
    snapshot_type: str
    schema_version: str
    schema_fingerprints: dict[str, str]
    source_name: str
    source_policy_version: str
    source_version: str
    source_competition_code: str
    source_season_code: str
    competition_id: str
    season_id: str
    source_url: str
    source_observed_at_utc: datetime
    raw_artifact_relative_path: str
    raw_artifact_checksum_sha256: str
    raw_artifact_bytes: int
    raw_encoding: str | None
    http_metadata: dict[str, JsonValue]
    parser_version: str
    normalizer_version: str
    python_version: str
    pyarrow_version: str
    files: tuple[ValidatedManifestFile, ...]
    files_by_dataset: dict[str, ValidatedManifestFile]
    row_counts: dict[str, int]
    unknown_source_columns: tuple[str, ...]
    missing_optional_source_columns: tuple[str, ...]
    duplicate_source_rows_discarded: int
    warnings: tuple[str, ...]
    quality_summary: ValidatedQualitySummary
    pinnacle_caution_quote_count: int
    generated_snapshot_relative_path: str
    games_count: int
    teams_count: int
    odds_quotes_count: int
    statistics_rows_count: int


def build_manifest_document(
    *,
    snapshot_id: str,
    source_name: str,
    source_version: str,
    source_competition_code: str,
    source_season_code: str,
    competition_id: str,
    season_id: str,
    source_url: str,
    source_observed_at_utc: datetime,
    raw_relative_path: str,
    raw_checksum_sha256: str,
    raw_bytes: int,
    raw_encoding: str | None,
    http_status: int | None,
    http_content_type: str | None,
    http_content_length: int | None,
    http_etag: str | None,
    http_last_modified: str | None,
    http_final_url: str | None,
    bundle: NormalizedFootballBundle,
    file_meta: dict[str, dict[str, object]],
    snapshot_relative_directory: str,
    unknown_source_columns: tuple[str, ...],
    missing_optional_source_columns: tuple[str, ...],
) -> dict[str, JsonValue]:
    """Build a canonical JSON-compatible manifest document."""
    schema_fingerprints = {
        dataset: schema_fingerprint(dataset_schema(dataset)) for dataset in CANONICAL_DATASETS
    }
    files: list[JsonValue] = [
        {
            "relative_filename": str(file_meta[dataset]["relative_filename"]),
            "sha256": str(file_meta[dataset]["sha256"]),
            "byte_count": int(file_meta[dataset]["byte_count"]),  # type: ignore[call-overload]
            "row_count": int(file_meta[dataset]["row_count"]),  # type: ignore[call-overload]
            "schema_fingerprint": str(file_meta[dataset]["schema_fingerprint"]),
        }
        for dataset in CANONICAL_DATASETS
    ]
    row_counts = {
        dataset: int(file_meta[dataset]["row_count"])  # type: ignore[call-overload]
        for dataset in CANONICAL_DATASETS
    }
    quality_summary: dict[str, JsonValue] = {
        "pinnacle_caution_quote_count": bundle.pinnacle_caution_quote_count,
        "duplicate_rows_discarded": bundle.duplicate_rows_discarded,
        "warnings_count": len(bundle.warnings),
    }
    http_metadata: dict[str, JsonValue] = {
        "status": http_status,
        "content_type": http_content_type,
        "content_length": http_content_length,
        "etag": http_etag,
        "last_modified": http_last_modified,
        "final_url": http_final_url,
    }
    return {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_type": FOOTBALL_INGESTION_SNAPSHOT_TYPE,
        "schema_version": FOOTBALL_CANONICAL_SCHEMA_VERSION,
        "schema_fingerprints": schema_fingerprints,
        "source_name": source_name,
        "source_policy_version": bundle.source_policy_version,
        "source_version": source_version,
        "source_competition_code": source_competition_code,
        "source_season_code": source_season_code,
        "competition_id": competition_id,
        "season_id": season_id,
        "source_url": source_url,
        "source_observed_at_utc": format_utc_timestamp(source_observed_at_utc),
        "raw_artifact_relative_path": raw_relative_path,
        "raw_artifact_checksum_sha256": raw_checksum_sha256,
        "raw_artifact_bytes": raw_bytes,
        "raw_encoding": raw_encoding,
        "http_metadata": http_metadata,
        "parser_version": FOOTBALL_PARSER_VERSION,
        "normalizer_version": FOOTBALL_NORMALIZER_VERSION,
        "python_version": platform.python_version(),
        "pyarrow_version": pa.__version__,
        "files": files,
        "row_counts": row_counts,
        "unknown_source_columns": list(sorted(unknown_source_columns)),
        "missing_optional_source_columns": list(sorted(missing_optional_source_columns)),
        "duplicate_source_rows_discarded": bundle.duplicate_rows_discarded,
        "warnings": list(sorted(bundle.warnings)),
        "quality_summary": quality_summary,
        "pinnacle_caution_quote_count": bundle.pinnacle_caution_quote_count,
        "generated_snapshot_relative_path": snapshot_relative_directory,
    }


def serialize_manifest(document: dict[str, JsonValue]) -> bytes:
    """Serialize a manifest document to canonical UTF-8 JSON with a final newline."""
    text = dumps_canonical_json(document)
    return (text + "\n").encode("utf-8")


def write_manifest(path: Path, document: dict[str, JsonValue]) -> tuple[bytes, str]:
    """Write ``manifest.json`` and return ``(bytes, sha256)``."""
    payload = serialize_manifest(document)
    path.write_bytes(payload)
    return payload, hashlib.sha256(payload).hexdigest()


def validate_manifest_document(document: object) -> ValidatedManifest:
    """Validate and type a decoded manifest document.

    Any malformed field is reported as ``SnapshotVerificationError`` so callers
    never observe raw parser/type exceptions while verifying untrusted snapshots.
    """
    try:
        return _validate_manifest_document(document)
    except SnapshotVerificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize all validation failures
        msg = "manifest validation failed"
        raise SnapshotVerificationError(msg) from exc


def load_manifest_bytes(path: Path) -> tuple[ValidatedManifest, bytes, str]:
    """Load and parse a manifest file, returning document, raw bytes, and checksum."""
    if path.is_symlink():
        msg = "manifest must not be a symlink"
        raise SnapshotVerificationError(msg)
    payload = path.read_bytes()
    if not payload.endswith(b"\n"):
        msg = "manifest must end with a newline"
        raise SnapshotVerificationError(msg)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "manifest is not valid UTF-8"
        raise SnapshotVerificationError(msg) from exc
    try:
        loaded = loads_canonical_json(text.rstrip("\n"))
    except Exception as exc:  # noqa: BLE001
        msg = "manifest JSON is malformed"
        raise SnapshotVerificationError(msg) from exc
    validated = validate_manifest_document(loaded)
    digest = hashlib.sha256(payload).hexdigest()
    return validated, payload, digest


def _validate_manifest_document(document: object) -> ValidatedManifest:
    doc = _as_object(document, "manifest root")
    manifest_version = _required_str(doc, "manifest_version")
    if manifest_version != MANIFEST_VERSION:
        msg = f"unsupported manifest_version: {manifest_version!r}"
        raise SnapshotVerificationError(msg)

    snapshot_id_value = _required_str(doc, "snapshot_id")
    snapshot_id = normalize_uuid(snapshot_id_value)
    if snapshot_id != snapshot_id_value:
        msg = "manifest snapshot_id must be a canonical lowercase UUID"
        raise SnapshotVerificationError(msg)

    snapshot_type = _identifier(doc, "snapshot_type")
    if snapshot_type != FOOTBALL_INGESTION_SNAPSHOT_TYPE:
        msg = "manifest snapshot_type is not supported"
        raise SnapshotVerificationError(msg)
    schema_version = _identifier(doc, "schema_version")
    if schema_version != FOOTBALL_CANONICAL_SCHEMA_VERSION:
        msg = "manifest schema_version is not supported"
        raise SnapshotVerificationError(msg)

    source_name = _identifier(doc, "source_name")
    source_policy_version = _identifier(doc, "source_policy_version")
    source_version = _identifier(doc, "source_version")
    competition_id = _identifier(doc, "competition_id")
    season_id = _identifier(doc, "season_id")
    parser_version = _identifier(doc, "parser_version")
    normalizer_version = _identifier(doc, "normalizer_version")
    source_competition_code = _required_str(doc, "source_competition_code")
    source_season_code = _required_str(doc, "source_season_code")
    source_url = _required_str(doc, "source_url")
    source_observed_at_utc = _utc_timestamp(doc, "source_observed_at_utc")
    raw_artifact_relative_path = _relative_path(doc, "raw_artifact_relative_path")
    raw_artifact_checksum_sha256 = _sha256(doc, "raw_artifact_checksum_sha256")
    raw_artifact_bytes = _bounded_int(
        _required(doc, "raw_artifact_bytes"),
        "raw_artifact_bytes",
    )
    raw_encoding = _optional_str(_required(doc, "raw_encoding"), "raw_encoding")
    http_metadata = _http_metadata(doc)
    python_version = _required_str(doc, "python_version")
    pyarrow_version = _required_str(doc, "pyarrow_version")

    schema_fingerprints = _schema_fingerprints(doc)
    files, files_by_dataset = _manifest_files(doc)
    row_counts = _row_counts(doc, files_by_dataset)
    unknown_source_columns = _string_tuple(doc, "unknown_source_columns")
    missing_optional_source_columns = _string_tuple(doc, "missing_optional_source_columns")
    warnings = _string_tuple(doc, "warnings")
    duplicate_source_rows_discarded = _bounded_int(
        _required(doc, "duplicate_source_rows_discarded"),
        "duplicate_source_rows_discarded",
    )
    pinnacle_caution_quote_count = _bounded_int(
        _required(doc, "pinnacle_caution_quote_count"),
        "pinnacle_caution_quote_count",
    )
    quality_summary = _quality_summary(doc)
    if duplicate_source_rows_discarded != quality_summary.duplicate_rows_discarded:
        msg = "manifest duplicate row counters disagree"
        raise SnapshotVerificationError(msg)
    if pinnacle_caution_quote_count != quality_summary.pinnacle_caution_quote_count:
        msg = "manifest pinnacle caution counters disagree"
        raise SnapshotVerificationError(msg)
    if len(warnings) != quality_summary.warnings_count:
        msg = "manifest warnings_count does not match warnings"
        raise SnapshotVerificationError(msg)

    generated_snapshot_relative_path = _relative_path(doc, "generated_snapshot_relative_path")
    return ValidatedManifest(
        document=doc,
        snapshot_id=snapshot_id,
        snapshot_type=snapshot_type,
        schema_version=schema_version,
        schema_fingerprints=schema_fingerprints,
        source_name=source_name,
        source_policy_version=source_policy_version,
        source_version=source_version,
        source_competition_code=source_competition_code,
        source_season_code=source_season_code,
        competition_id=competition_id,
        season_id=season_id,
        source_url=source_url,
        source_observed_at_utc=source_observed_at_utc,
        raw_artifact_relative_path=raw_artifact_relative_path,
        raw_artifact_checksum_sha256=raw_artifact_checksum_sha256,
        raw_artifact_bytes=raw_artifact_bytes,
        raw_encoding=raw_encoding,
        http_metadata=http_metadata,
        parser_version=parser_version,
        normalizer_version=normalizer_version,
        python_version=python_version,
        pyarrow_version=pyarrow_version,
        files=files,
        files_by_dataset=files_by_dataset,
        row_counts=row_counts,
        unknown_source_columns=unknown_source_columns,
        missing_optional_source_columns=missing_optional_source_columns,
        duplicate_source_rows_discarded=duplicate_source_rows_discarded,
        warnings=warnings,
        quality_summary=quality_summary,
        pinnacle_caution_quote_count=pinnacle_caution_quote_count,
        generated_snapshot_relative_path=generated_snapshot_relative_path,
        games_count=row_counts["games"],
        teams_count=row_counts["teams"],
        odds_quotes_count=row_counts["odds_1x2"],
        statistics_rows_count=row_counts["post_match_statistics"],
    )


def _as_object(value: object, field_name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"{field_name} must be an object"
        raise SnapshotVerificationError(msg)
    return cast(dict[str, JsonValue], value)


def _required(document: dict[str, JsonValue], key: str) -> JsonValue:
    if key not in document:
        msg = f"manifest missing required key: {key}"
        raise SnapshotVerificationError(msg)
    return document[key]


def _required_str(document: dict[str, JsonValue], key: str) -> str:
    value = _required(document, key)
    if not isinstance(value, str):
        msg = f"manifest {key} must be a string"
        raise SnapshotVerificationError(msg)
    if value != value.strip() or not value:
        msg = f"manifest {key} must be non-empty without surrounding whitespace"
        raise SnapshotVerificationError(msg)
    return value


def _optional_str(value: JsonValue, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"manifest {field_name} must be a string or null"
        raise SnapshotVerificationError(msg)
    if value != value.strip():
        msg = f"manifest {field_name} must not have surrounding whitespace"
        raise SnapshotVerificationError(msg)
    return value


def _identifier(document: dict[str, JsonValue], key: str) -> str:
    value = _required_str(document, key)
    try:
        return validate_identifier(value, field_name=key)
    except Exception as exc:  # noqa: BLE001 - repository validators are normalized here
        msg = f"manifest {key} is not a valid identifier"
        raise SnapshotVerificationError(msg) from exc


def _sha256(document: dict[str, JsonValue], key: str) -> str:
    value = _required_str(document, key)
    try:
        return validate_sha256_checksum(value)
    except Exception as exc:  # noqa: BLE001
        msg = f"manifest {key} must be a lowercase sha256 checksum"
        raise SnapshotVerificationError(msg) from exc


def _relative_path(document: dict[str, JsonValue], key: str) -> str:
    value = _required_str(document, key)
    try:
        return validate_relative_snapshot_path(value)
    except Exception as exc:  # noqa: BLE001
        msg = f"manifest {key} is not a valid relative path"
        raise SnapshotVerificationError(msg) from exc


def _bounded_int(value: JsonValue, field_name: str, *, maximum: int = MAX_MANIFEST_INT) -> int:
    if type(value) is not int:
        msg = f"manifest {field_name} must be an integer"
        raise SnapshotVerificationError(msg)
    if value < 0 or value > maximum:
        msg = f"manifest {field_name} must be between 0 and {maximum}"
        raise SnapshotVerificationError(msg)
    return value


def _utc_timestamp(document: dict[str, JsonValue], key: str) -> datetime:
    value = _required_str(document, key)
    try:
        return parse_utc_timestamp(value)
    except Exception as exc:  # noqa: BLE001
        msg = f"manifest {key} must be a canonical UTC timestamp"
        raise SnapshotVerificationError(msg) from exc


def _string_tuple(document: dict[str, JsonValue], key: str) -> tuple[str, ...]:
    value = _required(document, key)
    if not isinstance(value, list):
        msg = f"manifest {key} must be a list"
        raise SnapshotVerificationError(msg)
    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            msg = f"manifest {key}[{index}] must be a string"
            raise SnapshotVerificationError(msg)
        strings.append(item)
    return tuple(strings)


def _object_exact_keys(
    document: dict[str, JsonValue],
    key: str,
    expected_keys: frozenset[str],
) -> dict[str, JsonValue]:
    value = _as_object(_required(document, key), f"manifest {key}")
    actual = frozenset(value)
    if actual != expected_keys:
        missing = sorted(expected_keys - actual)
        unexpected = sorted(actual - expected_keys)
        msg = f"manifest {key} keys mismatch missing={missing} unexpected={unexpected}"
        raise SnapshotVerificationError(msg)
    return value


def _http_metadata(document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    metadata = _object_exact_keys(document, "http_metadata", _HTTP_METADATA_KEYS)
    status = metadata["status"]
    if status is not None:
        _bounded_int(status, "http_metadata.status", maximum=599)
        if cast(int, status) < 100:
            msg = "manifest http_metadata.status must be a valid HTTP status code"
            raise SnapshotVerificationError(msg)
    content_length = metadata["content_length"]
    if content_length is not None:
        _bounded_int(content_length, "http_metadata.content_length")
    for key in ("content_type", "etag", "last_modified", "final_url"):
        _optional_str(metadata[key], f"http_metadata.{key}")
    return metadata


def _schema_fingerprints(document: dict[str, JsonValue]) -> dict[str, str]:
    expected_keys = frozenset(CANONICAL_DATASETS)
    fingerprints = _object_exact_keys(document, "schema_fingerprints", expected_keys)
    validated: dict[str, str] = {}
    for dataset_name in CANONICAL_DATASETS:
        value = fingerprints[dataset_name]
        if not isinstance(value, str):
            msg = f"manifest schema_fingerprints.{dataset_name} must be a string"
            raise SnapshotVerificationError(msg)
        try:
            fingerprint = validate_sha256_checksum(value)
        except Exception as exc:  # noqa: BLE001
            msg = f"manifest schema_fingerprints.{dataset_name} must be lowercase sha256"
            raise SnapshotVerificationError(msg) from exc
        expected = schema_fingerprint(dataset_schema(dataset_name))
        if fingerprint != expected:
            msg = f"manifest schema fingerprint mismatch for {dataset_name}"
            raise SnapshotVerificationError(msg)
        validated[dataset_name] = fingerprint
    return validated


def _manifest_files(
    document: dict[str, JsonValue],
) -> tuple[tuple[ValidatedManifestFile, ...], dict[str, ValidatedManifestFile]]:
    value = _required(document, "files")
    if not isinstance(value, list):
        msg = "manifest files must be a list"
        raise SnapshotVerificationError(msg)
    if len(value) != len(CANONICAL_DATASETS):
        msg = "manifest files must contain exactly one entry per canonical dataset"
        raise SnapshotVerificationError(msg)

    dataset_by_filename = {PARQUET_FILENAMES[dataset]: dataset for dataset in CANONICAL_DATASETS}
    seen_filenames: set[str] = set()
    by_dataset: dict[str, ValidatedManifestFile] = {}
    for index, item in enumerate(value):
        entry = _as_object(item, f"manifest files[{index}]")
        filename = _nested_required_str(entry, "relative_filename", f"files[{index}]")
        if filename in seen_filenames:
            msg = f"manifest files contains duplicate entry for {filename}"
            raise SnapshotVerificationError(msg)
        seen_filenames.add(filename)
        dataset_name = dataset_by_filename.get(filename)
        if dataset_name is None:
            msg = f"manifest files contains unexpected filename: {filename}"
            raise SnapshotVerificationError(msg)
        sha256 = _nested_sha256(entry, "sha256", f"files[{index}]")
        byte_count = _bounded_int(
            _nested_required(entry, "byte_count", f"files[{index}]"),
            f"files[{index}].byte_count",
        )
        row_count = _bounded_int(
            _nested_required(entry, "row_count", f"files[{index}]"),
            f"files[{index}].row_count",
        )
        file_fingerprint = _nested_sha256(entry, "schema_fingerprint", f"files[{index}]")
        expected_fingerprint = schema_fingerprint(dataset_schema(dataset_name))
        if file_fingerprint != expected_fingerprint:
            msg = f"manifest file schema fingerprint mismatch for {dataset_name}"
            raise SnapshotVerificationError(msg)
        by_dataset[dataset_name] = ValidatedManifestFile(
            dataset_name=dataset_name,
            relative_filename=filename,
            sha256=sha256,
            byte_count=byte_count,
            row_count=row_count,
            schema_fingerprint=file_fingerprint,
        )
    if set(by_dataset) != set(CANONICAL_DATASETS):
        msg = "manifest files must exactly match canonical datasets"
        raise SnapshotVerificationError(msg)
    return tuple(by_dataset[dataset] for dataset in CANONICAL_DATASETS), by_dataset


def _nested_required(document: dict[str, JsonValue], key: str, parent: str) -> JsonValue:
    if key not in document:
        msg = f"manifest {parent} missing required key: {key}"
        raise SnapshotVerificationError(msg)
    return document[key]


def _nested_required_str(document: dict[str, JsonValue], key: str, parent: str) -> str:
    value = _nested_required(document, key, parent)
    if not isinstance(value, str):
        msg = f"manifest {parent}.{key} must be a string"
        raise SnapshotVerificationError(msg)
    if value != value.strip() or not value:
        msg = f"manifest {parent}.{key} must be non-empty without surrounding whitespace"
        raise SnapshotVerificationError(msg)
    return value


def _nested_sha256(document: dict[str, JsonValue], key: str, parent: str) -> str:
    value = _nested_required_str(document, key, parent)
    try:
        return validate_sha256_checksum(value)
    except Exception as exc:  # noqa: BLE001
        msg = f"manifest {parent}.{key} must be a lowercase sha256 checksum"
        raise SnapshotVerificationError(msg) from exc


def _row_counts(
    document: dict[str, JsonValue],
    files_by_dataset: dict[str, ValidatedManifestFile],
) -> dict[str, int]:
    counts = _object_exact_keys(document, "row_counts", frozenset(CANONICAL_DATASETS))
    validated: dict[str, int] = {}
    for dataset_name in CANONICAL_DATASETS:
        count = _bounded_int(counts[dataset_name], f"row_counts.{dataset_name}")
        if count != files_by_dataset[dataset_name].row_count:
            msg = f"manifest row_counts mismatch for {dataset_name}"
            raise SnapshotVerificationError(msg)
        validated[dataset_name] = count
    return validated


def _quality_summary(document: dict[str, JsonValue]) -> ValidatedQualitySummary:
    quality = _object_exact_keys(document, "quality_summary", _QUALITY_SUMMARY_KEYS)
    return ValidatedQualitySummary(
        pinnacle_caution_quote_count=_bounded_int(
            quality["pinnacle_caution_quote_count"],
            "quality_summary.pinnacle_caution_quote_count",
        ),
        duplicate_rows_discarded=_bounded_int(
            quality["duplicate_rows_discarded"],
            "quality_summary.duplicate_rows_discarded",
        ),
        warnings_count=_bounded_int(
            quality["warnings_count"],
            "quality_summary.warnings_count",
        ),
    )


def expected_parquet_filenames() -> frozenset[str]:
    """Return the exact set of Parquet filenames required in a snapshot directory."""
    return frozenset(PARQUET_FILENAMES[name] for name in CANONICAL_DATASETS)


def validate_manifest_identity(
    document: dict[str, JsonValue],
    *,
    snapshot_id: str,
    source_version: str,
    schema_version: str,
    competition_id: str,
    season_id: str,
) -> None:
    """Validate core identity fields in a loaded manifest."""
    checks = {
        "snapshot_id": snapshot_id,
        "source_version": source_version,
        "schema_version": schema_version,
        "competition_id": competition_id,
        "season_id": season_id,
        "snapshot_type": FOOTBALL_INGESTION_SNAPSHOT_TYPE,
    }
    for key, expected in checks.items():
        actual = document.get(key)
        if actual != expected:
            msg = f"manifest identity mismatch for {key}"
            raise SnapshotIntegrityError(msg)

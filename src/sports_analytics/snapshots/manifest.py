"""Canonical snapshot manifest construction and serialization."""

from __future__ import annotations

import hashlib
import platform
from datetime import datetime
from pathlib import Path

import pyarrow as pa

from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    format_utc_timestamp,
    loads_canonical_json,
)
from sports_analytics.data.types import JsonValue
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


def load_manifest_bytes(path: Path) -> tuple[dict[str, JsonValue], bytes, str]:
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
    if not isinstance(loaded, dict):
        msg = "manifest root must be an object"
        raise SnapshotVerificationError(msg)
    if loaded.get("manifest_version") != MANIFEST_VERSION:
        msg = f"unsupported manifest_version: {loaded.get('manifest_version')!r}"
        raise SnapshotVerificationError(msg)
    digest = hashlib.sha256(payload).hexdigest()
    return loaded, payload, digest


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

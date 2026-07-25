"""Sport-agnostic snapshot manifest construction and hostile-input validation.

Every untrusted manifest value passes through a typed validation layer before it
is used. Malformed manifests always surface as ``SnapshotVerificationError`` so
callers never observe ``KeyError``, ``TypeError``, ``ValueError``,
``OverflowError``, Arrow exceptions, or raw JSON decoder exceptions.
"""

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
from sports_analytics.snapshots.spec import (
    MANIFEST_VERSION,
    SnapshotDatasetSuite,
    SnapshotSpec,
    validate_partition_value,
)

MAX_MANIFEST_INT: Final[int] = 2**63 - 1
MAX_MANIFEST_TEXT_LENGTH: Final[int] = 2_048
_HTTP_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "network_retrieved",
        "status",
        "content_type",
        "content_length",
        "etag",
        "last_modified",
        "final_url",
    }
)
_RAW_ARTIFACT_KEYS: Final[frozenset[str]] = frozenset(
    {"relative_path", "checksum_sha256", "byte_count", "encoding"}
)
_FILE_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {"relative_filename", "sha256", "byte_count", "row_count", "schema_fingerprint"}
)
_REQUIRED_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
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


@dataclass(frozen=True, slots=True)
class ValidatedManifestFile:
    """Typed metadata for one expected Parquet file entry."""

    dataset_name: str
    relative_filename: str
    sha256: str
    byte_count: int
    row_count: int
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class ValidatedRawArtifact:
    """Typed raw artifact reference from a manifest."""

    relative_path: str
    checksum_sha256: str
    byte_count: int
    encoding: str | None


@dataclass(frozen=True, slots=True)
class ValidatedHttpMetadata:
    """Typed HTTP metadata from a manifest."""

    network_retrieved: bool
    status: int | None
    content_type: str | None
    content_length: int | None
    etag: str | None
    last_modified: str | None
    final_url: str | None


@dataclass(frozen=True, slots=True)
class ValidatedManifest:
    """A manifest document validated for read-only verification/publication."""

    document: dict[str, JsonValue]
    manifest_version: str
    snapshot_id: str
    snapshot_type: str
    schema_version: str
    source_name: str
    source_version: str
    source_policy_version: str
    source_url: str
    source_observed_at_utc: datetime
    partition_keys: tuple[tuple[str, str], ...]
    domain_metadata: dict[str, JsonValue]
    producer_versions: dict[str, str]
    raw_artifact: ValidatedRawArtifact
    http_metadata: ValidatedHttpMetadata
    python_version: str
    pyarrow_version: str
    schema_fingerprints: dict[str, str]
    files: tuple[ValidatedManifestFile, ...]
    files_by_dataset: dict[str, ValidatedManifestFile]
    row_counts: dict[str, int]
    quality_summary: dict[str, int]
    warnings: tuple[str, ...]
    generated_snapshot_relative_path: str


def build_manifest_document(
    *,
    snapshot_id: str,
    spec: SnapshotSpec,
    file_meta: dict[str, dict[str, object]],
    snapshot_relative_directory: str,
) -> dict[str, JsonValue]:
    """Build a canonical JSON-compatible manifest document from a snapshot spec."""
    suite = spec.suite
    files: list[JsonValue] = []
    row_counts: dict[str, JsonValue] = {}
    for dataset_name in suite.dataset_names:
        meta = file_meta[dataset_name]
        row_count = _as_int(meta["row_count"], field_name=f"{dataset_name}.row_count")
        files.append(
            {
                "relative_filename": str(meta["relative_filename"]),
                "sha256": str(meta["sha256"]),
                "byte_count": _as_int(meta["byte_count"], field_name=f"{dataset_name}.byte_count"),
                "row_count": row_count,
                "schema_fingerprint": str(meta["schema_fingerprint"]),
            }
        )
        row_counts[dataset_name] = row_count
    quality_summary: dict[str, JsonValue] = {
        key: spec.quality_summary[key] for key in sorted(spec.quality_summary)
    }
    producer_versions: dict[str, JsonValue] = {
        key: spec.producer_versions[key] for key in sorted(spec.producer_versions)
    }
    domain_metadata: dict[str, JsonValue] = {
        key: spec.domain_metadata[key] for key in sorted(spec.domain_metadata)
    }
    fingerprints: dict[str, JsonValue] = dict(suite.schema_fingerprints())
    partition_keys: dict[str, JsonValue] = dict(spec.identity.partition_mapping)
    return {
        "manifest_version": MANIFEST_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_type": spec.identity.snapshot_type,
        "schema_version": spec.identity.schema_version,
        "source_name": spec.identity.source_name,
        "source_version": spec.identity.source_version,
        "source_policy_version": spec.source_policy_version,
        "source_url": spec.source_url,
        "source_observed_at_utc": format_utc_timestamp(spec.source_observed_at_utc),
        "partition_keys": partition_keys,
        "domain_metadata": domain_metadata,
        "producer_versions": producer_versions,
        "raw_artifact": {
            "relative_path": spec.raw_artifact.relative_path,
            "checksum_sha256": spec.raw_artifact.checksum_sha256,
            "byte_count": spec.raw_artifact.byte_count,
            "encoding": spec.raw_artifact.encoding,
        },
        "http_metadata": spec.http_metadata.to_document(),
        "python_version": platform.python_version(),
        "pyarrow_version": pa.__version__,
        "schema_fingerprints": fingerprints,
        "files": files,
        "row_counts": row_counts,
        "quality_summary": quality_summary,
        "warnings": list(sorted(spec.warnings)),
        "generated_snapshot_relative_path": snapshot_relative_directory,
    }


def _as_int(value: object, *, field_name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_MANIFEST_INT:
        msg = f"manifest {field_name} must be a bounded non-negative int"
        raise SnapshotIntegrityError(msg)
    return value


def serialize_manifest(document: dict[str, JsonValue]) -> bytes:
    """Serialize a manifest document to canonical UTF-8 JSON with a final newline."""
    text = dumps_canonical_json(document)
    return (text + "\n").encode("utf-8")


def write_manifest(path: Path, document: dict[str, JsonValue]) -> tuple[bytes, str]:
    """Write ``manifest.json`` and return ``(bytes, sha256)``."""
    payload = serialize_manifest(document)
    path.write_bytes(payload)
    return payload, hashlib.sha256(payload).hexdigest()


def validate_manifest_document(
    document: object,
    *,
    suite: SnapshotDatasetSuite,
) -> ValidatedManifest:
    """Validate and type a decoded manifest document against an expected suite."""
    try:
        return _validate_manifest_document(document, suite=suite)
    except SnapshotVerificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize all validation failures
        msg = "manifest validation failed"
        raise SnapshotVerificationError(msg) from exc


def load_manifest_bytes(
    path: Path,
    *,
    suite: SnapshotDatasetSuite,
) -> tuple[ValidatedManifest, bytes, str]:
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
    validated = validate_manifest_document(loaded, suite=suite)
    digest = hashlib.sha256(payload).hexdigest()
    return validated, payload, digest


def _validate_manifest_document(
    document: object,
    *,
    suite: SnapshotDatasetSuite,
) -> ValidatedManifest:
    doc = _as_object(document, "manifest root")
    missing = sorted(_REQUIRED_TOP_LEVEL_KEYS - frozenset(doc))
    if missing:
        msg = f"manifest missing required keys: {', '.join(missing)}"
        raise SnapshotVerificationError(msg)

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
    schema_version = _identifier(doc, "schema_version")
    source_name = _identifier(doc, "source_name")
    source_version = _identifier(doc, "source_version")
    source_policy_version = _identifier(doc, "source_policy_version")
    source_url = _bounded_text(doc, "source_url")
    source_observed_at_utc = _utc_timestamp(doc, "source_observed_at_utc")
    partition_keys = _partition_keys(doc)
    domain_metadata = _domain_metadata(doc)
    producer_versions = _producer_versions(doc)
    raw_artifact = _raw_artifact(doc)
    http_metadata = _http_metadata(doc)
    python_version = _bounded_text(doc, "python_version")
    pyarrow_version = _bounded_text(doc, "pyarrow_version")
    schema_fingerprints = _schema_fingerprints(doc, suite=suite)
    files, files_by_dataset = _manifest_files(doc, suite=suite)
    row_counts = _row_counts(doc, files_by_dataset, suite=suite)
    quality_summary = _quality_summary(doc)
    warnings = _string_tuple(doc, "warnings")
    generated_snapshot_relative_path = _relative_path(doc, "generated_snapshot_relative_path")

    return ValidatedManifest(
        document=doc,
        manifest_version=manifest_version,
        snapshot_id=snapshot_id,
        snapshot_type=snapshot_type,
        schema_version=schema_version,
        source_name=source_name,
        source_version=source_version,
        source_policy_version=source_policy_version,
        source_url=source_url,
        source_observed_at_utc=source_observed_at_utc,
        partition_keys=partition_keys,
        domain_metadata=domain_metadata,
        producer_versions=producer_versions,
        raw_artifact=raw_artifact,
        http_metadata=http_metadata,
        python_version=python_version,
        pyarrow_version=pyarrow_version,
        schema_fingerprints=schema_fingerprints,
        files=files,
        files_by_dataset=files_by_dataset,
        row_counts=row_counts,
        quality_summary=quality_summary,
        warnings=warnings,
        generated_snapshot_relative_path=generated_snapshot_relative_path,
    )


def _as_object(value: object, field_name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"{field_name} must be an object"
        raise SnapshotVerificationError(msg)
    for key in value:
        if not isinstance(key, str):
            msg = f"{field_name} keys must be strings"
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


def _bounded_text(document: dict[str, JsonValue], key: str) -> str:
    value = _required_str(document, key)
    if len(value) > MAX_MANIFEST_TEXT_LENGTH:
        msg = f"manifest {key} exceeds maximum length of {MAX_MANIFEST_TEXT_LENGTH}"
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
    if len(value) > MAX_MANIFEST_TEXT_LENGTH:
        msg = f"manifest {field_name} exceeds maximum length of {MAX_MANIFEST_TEXT_LENGTH}"
        raise SnapshotVerificationError(msg)
    return value


def _identifier(document: dict[str, JsonValue], key: str) -> str:
    value = _required_str(document, key)
    try:
        return validate_identifier(value, field_name=key)
    except Exception as exc:  # noqa: BLE001 - repository validators are normalized here
        msg = f"manifest {key} is not a valid identifier"
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
        if len(item) > MAX_MANIFEST_TEXT_LENGTH:
            msg = f"manifest {key}[{index}] exceeds maximum length"
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


def _partition_keys(document: dict[str, JsonValue]) -> tuple[tuple[str, str], ...]:
    value = _as_object(_required(document, "partition_keys"), "manifest partition_keys")
    if not value:
        msg = "manifest partition_keys must not be empty"
        raise SnapshotVerificationError(msg)
    pairs: list[tuple[str, str]] = []
    for key in sorted(value):
        try:
            validate_identifier(key, field_name="partition key")
        except Exception as exc:  # noqa: BLE001
            msg = f"manifest partition key {key!r} is not a valid identifier"
            raise SnapshotVerificationError(msg) from exc
        item = value[key]
        if not isinstance(item, str):
            msg = f"manifest partition_keys.{key} must be a string"
            raise SnapshotVerificationError(msg)
        try:
            validate_partition_value(item, field_name=key)
        except SnapshotIntegrityError as exc:
            raise SnapshotVerificationError(str(exc)) from exc
        pairs.append((key, item))
    return tuple(pairs)


def _domain_metadata(document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = _as_object(_required(document, "domain_metadata"), "manifest domain_metadata")
    validated: dict[str, JsonValue] = {}
    for key in sorted(value):
        try:
            validate_identifier(key, field_name="domain_metadata key")
        except Exception as exc:  # noqa: BLE001
            msg = f"manifest domain_metadata key {key!r} is not a valid identifier"
            raise SnapshotVerificationError(msg) from exc
        item = value[key]
        if item is None or isinstance(item, bool):
            validated[key] = item
            continue
        if type(item) is int:
            validated[key] = _bounded_int(item, f"domain_metadata.{key}")
            continue
        if isinstance(item, str):
            validated[key] = _optional_str(item, f"domain_metadata.{key}")
            continue
        if isinstance(item, list):
            entries: list[JsonValue] = []
            for index, entry in enumerate(item):
                if not isinstance(entry, str):
                    msg = f"manifest domain_metadata.{key}[{index}] must be a string"
                    raise SnapshotVerificationError(msg)
                if len(entry) > MAX_MANIFEST_TEXT_LENGTH:
                    msg = f"manifest domain_metadata.{key}[{index}] exceeds maximum length"
                    raise SnapshotVerificationError(msg)
                entries.append(entry)
            validated[key] = entries
            continue
        msg = (
            f"manifest domain_metadata.{key} must be a string, int, bool, list of strings, or null"
        )
        raise SnapshotVerificationError(msg)
    return validated


def _producer_versions(document: dict[str, JsonValue]) -> dict[str, str]:
    value = _as_object(_required(document, "producer_versions"), "manifest producer_versions")
    validated: dict[str, str] = {}
    for key in sorted(value):
        item = value[key]
        if not isinstance(item, str):
            msg = f"manifest producer_versions.{key} must be a string"
            raise SnapshotVerificationError(msg)
        try:
            validate_identifier(key, field_name="producer_versions key")
            validated[key] = validate_identifier(item, field_name=f"producer_versions.{key}")
        except Exception as exc:  # noqa: BLE001
            msg = f"manifest producer_versions.{key} is not a valid identifier"
            raise SnapshotVerificationError(msg) from exc
    return validated


def _raw_artifact(document: dict[str, JsonValue]) -> ValidatedRawArtifact:
    value = _object_exact_keys(document, "raw_artifact", _RAW_ARTIFACT_KEYS)
    relative_path = value["relative_path"]
    if not isinstance(relative_path, str):
        msg = "manifest raw_artifact.relative_path must be a string"
        raise SnapshotVerificationError(msg)
    try:
        validated_path = validate_relative_snapshot_path(relative_path)
    except Exception as exc:  # noqa: BLE001
        msg = "manifest raw_artifact.relative_path is not a valid relative path"
        raise SnapshotVerificationError(msg) from exc
    checksum = value["checksum_sha256"]
    if not isinstance(checksum, str):
        msg = "manifest raw_artifact.checksum_sha256 must be a string"
        raise SnapshotVerificationError(msg)
    try:
        validated_checksum = validate_sha256_checksum(checksum)
    except Exception as exc:  # noqa: BLE001
        msg = "manifest raw_artifact.checksum_sha256 must be lowercase sha256"
        raise SnapshotVerificationError(msg) from exc
    return ValidatedRawArtifact(
        relative_path=validated_path,
        checksum_sha256=validated_checksum,
        byte_count=_bounded_int(value["byte_count"], "raw_artifact.byte_count"),
        encoding=_optional_str(value["encoding"], "raw_artifact.encoding"),
    )


def _http_metadata(document: dict[str, JsonValue]) -> ValidatedHttpMetadata:
    metadata = _object_exact_keys(document, "http_metadata", _HTTP_METADATA_KEYS)
    network_retrieved = metadata["network_retrieved"]
    if not isinstance(network_retrieved, bool):
        msg = "manifest http_metadata.network_retrieved must be a boolean"
        raise SnapshotVerificationError(msg)
    status_value = metadata["status"]
    status: int | None = None
    if status_value is not None:
        status = _bounded_int(status_value, "http_metadata.status", maximum=599)
        if status < 100:
            msg = "manifest http_metadata.status must be a valid HTTP status code"
            raise SnapshotVerificationError(msg)
    content_length_value = metadata["content_length"]
    content_length: int | None = None
    if content_length_value is not None:
        content_length = _bounded_int(content_length_value, "http_metadata.content_length")
    validated = ValidatedHttpMetadata(
        network_retrieved=network_retrieved,
        status=status,
        content_type=_optional_str(metadata["content_type"], "http_metadata.content_type"),
        content_length=content_length,
        etag=_optional_str(metadata["etag"], "http_metadata.etag"),
        last_modified=_optional_str(metadata["last_modified"], "http_metadata.last_modified"),
        final_url=_optional_str(metadata["final_url"], "http_metadata.final_url"),
    )
    if not validated.network_retrieved and any(
        item is not None
        for item in (
            validated.status,
            validated.content_type,
            validated.content_length,
            validated.etag,
            validated.last_modified,
            validated.final_url,
        )
    ):
        msg = "manifest http_metadata records response fields without a network request"
        raise SnapshotVerificationError(msg)
    return validated


def _schema_fingerprints(
    document: dict[str, JsonValue],
    *,
    suite: SnapshotDatasetSuite,
) -> dict[str, str]:
    expected = suite.schema_fingerprints()
    fingerprints = _object_exact_keys(
        document,
        "schema_fingerprints",
        frozenset(expected),
    )
    validated: dict[str, str] = {}
    for dataset_name in suite.dataset_names:
        value = fingerprints[dataset_name]
        if not isinstance(value, str):
            msg = f"manifest schema_fingerprints.{dataset_name} must be a string"
            raise SnapshotVerificationError(msg)
        try:
            fingerprint = validate_sha256_checksum(value)
        except Exception as exc:  # noqa: BLE001
            msg = f"manifest schema_fingerprints.{dataset_name} must be lowercase sha256"
            raise SnapshotVerificationError(msg) from exc
        if fingerprint != expected[dataset_name]:
            msg = f"manifest schema fingerprint mismatch for {dataset_name}"
            raise SnapshotVerificationError(msg)
        validated[dataset_name] = fingerprint
    return validated


def _manifest_files(
    document: dict[str, JsonValue],
    *,
    suite: SnapshotDatasetSuite,
) -> tuple[tuple[ValidatedManifestFile, ...], dict[str, ValidatedManifestFile]]:
    value = _required(document, "files")
    if not isinstance(value, list):
        msg = "manifest files must be a list"
        raise SnapshotVerificationError(msg)
    if len(value) != len(suite.descriptors):
        msg = "manifest files must contain exactly one entry per expected dataset"
        raise SnapshotVerificationError(msg)

    seen_filenames: set[str] = set()
    by_dataset: dict[str, ValidatedManifestFile] = {}
    for index, item in enumerate(value):
        entry = _as_object(item, f"manifest files[{index}]")
        actual_keys = frozenset(entry)
        if actual_keys != _FILE_ENTRY_KEYS:
            missing = sorted(_FILE_ENTRY_KEYS - actual_keys)
            unexpected = sorted(actual_keys - _FILE_ENTRY_KEYS)
            msg = f"manifest files[{index}] keys mismatch missing={missing} unexpected={unexpected}"
            raise SnapshotVerificationError(msg)
        filename = _nested_required_str(entry, "relative_filename", f"files[{index}]")
        if filename in seen_filenames:
            msg = f"manifest files contains duplicate entry for {filename}"
            raise SnapshotVerificationError(msg)
        seen_filenames.add(filename)
        descriptor = suite.descriptor_for_filename(filename)
        if descriptor is None:
            msg = f"manifest files contains unexpected filename: {filename}"
            raise SnapshotVerificationError(msg)
        sha256 = _nested_sha256(entry, "sha256", f"files[{index}]")
        byte_count = _bounded_int(entry["byte_count"], f"files[{index}].byte_count")
        row_count = _bounded_int(entry["row_count"], f"files[{index}].row_count")
        file_fingerprint = _nested_sha256(entry, "schema_fingerprint", f"files[{index}]")
        if file_fingerprint != descriptor.schema_fingerprint:
            msg = f"manifest file schema fingerprint mismatch for {descriptor.dataset_name}"
            raise SnapshotVerificationError(msg)
        by_dataset[descriptor.dataset_name] = ValidatedManifestFile(
            dataset_name=descriptor.dataset_name,
            relative_filename=filename,
            sha256=sha256,
            byte_count=byte_count,
            row_count=row_count,
            schema_fingerprint=file_fingerprint,
        )
    if set(by_dataset) != set(suite.dataset_names):
        msg = "manifest files must exactly match the expected datasets"
        raise SnapshotVerificationError(msg)
    return tuple(by_dataset[name] for name in suite.dataset_names), by_dataset


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
    if len(value) > MAX_MANIFEST_TEXT_LENGTH:
        msg = f"manifest {parent}.{key} exceeds maximum length"
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
    *,
    suite: SnapshotDatasetSuite,
) -> dict[str, int]:
    counts = _object_exact_keys(document, "row_counts", frozenset(suite.dataset_names))
    validated: dict[str, int] = {}
    for dataset_name in suite.dataset_names:
        count = _bounded_int(counts[dataset_name], f"row_counts.{dataset_name}")
        if count != files_by_dataset[dataset_name].row_count:
            msg = f"manifest row_counts mismatch for {dataset_name}"
            raise SnapshotVerificationError(msg)
        validated[dataset_name] = count
    return validated


def _quality_summary(document: dict[str, JsonValue]) -> dict[str, int]:
    value = _as_object(_required(document, "quality_summary"), "manifest quality_summary")
    validated: dict[str, int] = {}
    for key in sorted(value):
        try:
            validate_identifier(key, field_name="quality_summary key")
        except Exception as exc:  # noqa: BLE001
            msg = f"manifest quality_summary key {key!r} is not a valid identifier"
            raise SnapshotVerificationError(msg) from exc
        validated[key] = _bounded_int(value[key], f"quality_summary.{key}")
    return validated


def validate_manifest_identity(
    manifest: ValidatedManifest,
    *,
    snapshot_id: str,
    snapshot_type: str,
    schema_version: str,
    source_name: str,
    source_version: str,
    partition_keys: tuple[tuple[str, str], ...],
) -> None:
    """Validate core identity fields of a loaded manifest."""
    checks: tuple[tuple[str, object, object], ...] = (
        ("snapshot_id", manifest.snapshot_id, snapshot_id),
        ("snapshot_type", manifest.snapshot_type, snapshot_type),
        ("schema_version", manifest.schema_version, schema_version),
        ("source_name", manifest.source_name, source_name),
        ("source_version", manifest.source_version, source_version),
        ("partition_keys", manifest.partition_keys, tuple(sorted(partition_keys))),
    )
    for key, actual, expected in checks:
        if actual != expected:
            msg = f"manifest identity mismatch for {key}"
            raise SnapshotIntegrityError(msg)

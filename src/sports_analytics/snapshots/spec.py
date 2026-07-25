"""Sport-agnostic snapshot specification contracts.

The shared snapshot infrastructure receives every domain-specific fact through a
validated specification. It therefore never imports sport, competition, season,
market, or dataset constants from a sport package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final

import pyarrow as pa

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.data.types import (
    JsonValue,
    validate_identifier,
    validate_relative_snapshot_path,
    validate_sha256_checksum,
)
from sports_analytics.snapshots.arrow import schema_fingerprint

MANIFEST_FILENAME: Final[str] = "manifest.json"
MANIFEST_VERSION: Final[str] = "snapshot-manifest-v2"

MAX_PARTITION_KEYS: Final[int] = 8
MAX_DATASETS: Final[int] = 32
MAX_SNAPSHOT_INT: Final[int] = 2**63 - 1

# Partition values become directory names on every supported platform, so colons,
# dots, and separators are rejected even though identifiers allow some of them.
_PARTITION_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DATASET_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9][a-z0-9_-]{0,63}\.parquet$"
)


def _identifier(value: str, *, field_name: str) -> str:
    try:
        return validate_identifier(value, field_name=field_name)
    except Exception as exc:  # noqa: BLE001 - normalized to a snapshot error
        msg = f"snapshot {field_name} is not a valid identifier"
        raise SnapshotIntegrityError(msg) from exc


def validate_partition_value(value: str, *, field_name: str) -> str:
    """Validate a partition value usable as a directory name on every platform."""
    if not isinstance(value, str) or _PARTITION_VALUE_PATTERN.fullmatch(value) is None:
        msg = (
            f"snapshot partition value for {field_name} must be a lowercase "
            "path-safe token of letters, digits, underscore, or hyphen"
        )
        raise SnapshotIntegrityError(msg)
    return value


@dataclass(frozen=True, slots=True)
class DatasetDescriptor:
    """One expected dataset in a snapshot directory."""

    dataset_name: str
    relative_filename: str
    schema: pa.Schema

    def __post_init__(self) -> None:
        _identifier(self.dataset_name, field_name="dataset_name")
        if _DATASET_FILENAME_PATTERN.fullmatch(self.relative_filename) is None:
            msg = f"snapshot dataset filename is not project-owned: {self.relative_filename}"
            raise SnapshotIntegrityError(msg)
        if not isinstance(self.schema, pa.Schema):
            msg = f"dataset {self.dataset_name} requires an Arrow schema"
            raise SnapshotIntegrityError(msg)

    @property
    def schema_fingerprint(self) -> str:
        """Return the deterministic fingerprint of the expected Arrow schema."""
        return schema_fingerprint(self.schema)


@dataclass(frozen=True, slots=True)
class SnapshotDatasetSuite:
    """The complete, ordered set of datasets a snapshot type must contain."""

    descriptors: tuple[DatasetDescriptor, ...]
    primary_dataset_name: str

    def __post_init__(self) -> None:
        if not self.descriptors:
            msg = "snapshot dataset suite must contain at least one dataset"
            raise SnapshotIntegrityError(msg)
        if len(self.descriptors) > MAX_DATASETS:
            msg = f"snapshot dataset suite exceeds {MAX_DATASETS} datasets"
            raise SnapshotIntegrityError(msg)
        names = [item.dataset_name for item in self.descriptors]
        filenames = [item.relative_filename for item in self.descriptors]
        if len(set(names)) != len(names):
            msg = "snapshot dataset suite contains duplicate dataset names"
            raise SnapshotIntegrityError(msg)
        if len(set(filenames)) != len(filenames):
            msg = "snapshot dataset suite contains duplicate filenames"
            raise SnapshotIntegrityError(msg)
        if MANIFEST_FILENAME in filenames:
            msg = "snapshot dataset filenames must not shadow the manifest"
            raise SnapshotIntegrityError(msg)
        if self.primary_dataset_name not in names:
            msg = "snapshot primary dataset must be part of the suite"
            raise SnapshotIntegrityError(msg)

    @property
    def dataset_names(self) -> tuple[str, ...]:
        """Return dataset names in declared order."""
        return tuple(item.dataset_name for item in self.descriptors)

    @property
    def filenames(self) -> frozenset[str]:
        """Return the exact set of Parquet filenames expected in a snapshot."""
        return frozenset(item.relative_filename for item in self.descriptors)

    @property
    def expected_directory_files(self) -> frozenset[str]:
        """Return every filename a complete snapshot directory must contain."""
        return self.filenames | {MANIFEST_FILENAME}

    def descriptor(self, dataset_name: str) -> DatasetDescriptor:
        """Return one descriptor by dataset name."""
        for item in self.descriptors:
            if item.dataset_name == dataset_name:
                return item
        msg = f"unknown snapshot dataset: {dataset_name}"
        raise SnapshotIntegrityError(msg)

    def descriptor_for_filename(self, filename: str) -> DatasetDescriptor | None:
        """Return the descriptor owning ``filename`` when it is expected."""
        for item in self.descriptors:
            if item.relative_filename == filename:
                return item
        return None

    def schema_fingerprints(self) -> dict[str, str]:
        """Return dataset name to expected schema fingerprint."""
        return {item.dataset_name: item.schema_fingerprint for item in self.descriptors}


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """The identity that makes a snapshot unique and discoverable."""

    snapshot_type: str
    schema_version: str
    source_name: str
    source_version: str
    partition_keys: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _identifier(self.snapshot_type, field_name="snapshot_type")
        _identifier(self.schema_version, field_name="schema_version")
        _identifier(self.source_name, field_name="source_name")
        _identifier(self.source_version, field_name="source_version")
        if not self.partition_keys:
            msg = "snapshot identity requires at least one partition key"
            raise SnapshotIntegrityError(msg)
        if len(self.partition_keys) > MAX_PARTITION_KEYS:
            msg = f"snapshot identity exceeds {MAX_PARTITION_KEYS} partition keys"
            raise SnapshotIntegrityError(msg)
        seen: set[str] = set()
        for key, value in self.partition_keys:
            _identifier(key, field_name="partition_key")
            validate_partition_value(value, field_name=key)
            if key in seen:
                msg = f"duplicate snapshot partition key: {key}"
                raise SnapshotIntegrityError(msg)
            seen.add(key)

    @property
    def partition_values(self) -> tuple[str, ...]:
        """Return ordered partition values used to build the storage path."""
        return tuple(value for _key, value in self.partition_keys)

    @property
    def partition_mapping(self) -> dict[str, str]:
        """Return partition keys as a mapping for manifest serialization."""
        return {key: value for key, value in self.partition_keys}

    def relative_parent_directory(self) -> str:
        """Return the relative parent directory that holds snapshot UUID children."""
        relative = PurePosixPath(
            self.snapshot_type,
            self.schema_version,
            *self.partition_values,
        ).as_posix()
        return validate_relative_snapshot_path(relative)

    def relative_directory(self, snapshot_id: str) -> str:
        """Return the relative snapshot directory for one snapshot UUID."""
        relative = PurePosixPath(self.relative_parent_directory(), snapshot_id).as_posix()
        return validate_relative_snapshot_path(relative)

    def relative_manifest_path(self, snapshot_id: str) -> str:
        """Return the relative manifest path for one snapshot UUID."""
        relative = PurePosixPath(
            self.relative_directory(snapshot_id),
            MANIFEST_FILENAME,
        ).as_posix()
        return validate_relative_snapshot_path(relative)


@dataclass(frozen=True, slots=True)
class RawArtifactReference:
    """Content-addressed raw artifact backing a snapshot."""

    relative_path: str
    checksum_sha256: str
    byte_count: int
    encoding: str | None

    def __post_init__(self) -> None:
        try:
            validate_relative_snapshot_path(self.relative_path)
            validate_sha256_checksum(self.checksum_sha256)
        except Exception as exc:  # noqa: BLE001
            msg = "snapshot raw artifact reference is invalid"
            raise SnapshotIntegrityError(msg) from exc
        if type(self.byte_count) is not int or not 0 <= self.byte_count <= MAX_SNAPSHOT_INT:
            msg = "snapshot raw artifact byte_count must be a bounded non-negative int"
            raise SnapshotIntegrityError(msg)


@dataclass(frozen=True, slots=True)
class SnapshotHttpMetadata:
    """Typed HTTP metadata for the retrieval that produced a snapshot.

    ``network_retrieved`` is ``False`` for cached raw execution; in that case
    every response field is ``None`` because no HTTP request occurred.
    """

    network_retrieved: bool
    status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    final_url: str | None = None

    def __post_init__(self) -> None:
        if not self.network_retrieved and any(
            value is not None
            for value in (
                self.status,
                self.content_type,
                self.content_length,
                self.etag,
                self.last_modified,
                self.final_url,
            )
        ):
            msg = "cached snapshot acquisition must not record HTTP response fields"
            raise SnapshotIntegrityError(msg)

    def to_document(self) -> dict[str, JsonValue]:
        """Return the canonical manifest representation."""
        return {
            "network_retrieved": self.network_retrieved,
            "status": self.status,
            "content_type": self.content_type,
            "content_length": self.content_length,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "final_url": self.final_url,
        }


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    """Everything the shared snapshot service needs about one publication."""

    identity: SnapshotIdentity
    suite: SnapshotDatasetSuite
    source_url: str
    source_policy_version: str
    source_observed_at_utc: datetime
    raw_artifact: RawArtifactReference
    http_metadata: SnapshotHttpMetadata
    producer_versions: dict[str, str] = field(default_factory=dict)
    domain_metadata: dict[str, JsonValue] = field(default_factory=dict)
    quality_summary: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_observed_at_utc.tzinfo is None:
            msg = "snapshot source_observed_at_utc must be timezone-aware"
            raise SnapshotIntegrityError(msg)
        _identifier(self.source_policy_version, field_name="source_policy_version")
        for version_key, version_value in self.producer_versions.items():
            _identifier(version_key, field_name="producer_version_key")
            _identifier(version_value, field_name=f"producer_version.{version_key}")
        for quality_key, quality_value in self.quality_summary.items():
            _identifier(quality_key, field_name="quality_summary_key")
            if not isinstance(quality_value, int) or isinstance(quality_value, bool):
                msg = f"quality_summary.{quality_key} must be an int"
                raise SnapshotIntegrityError(msg)
            if not 0 <= quality_value <= MAX_SNAPSHOT_INT:
                msg = f"quality_summary.{quality_key} must be a bounded non-negative int"
                raise SnapshotIntegrityError(msg)
        for key in self.domain_metadata:
            _identifier(key, field_name="domain_metadata_key")


@dataclass(frozen=True, slots=True)
class SnapshotMetrics:
    """Generic snapshot size and quality metrics."""

    row_counts: tuple[tuple[str, int], ...]
    file_count: int
    byte_count: int
    quality_summary: tuple[tuple[str, int], ...]
    warnings_count: int

    def row_count(self, dataset_name: str) -> int:
        """Return the row count for one dataset."""
        for name, count in self.row_counts:
            if name == dataset_name:
                return count
        msg = f"unknown snapshot dataset: {dataset_name}"
        raise SnapshotIntegrityError(msg)

    def row_count_mapping(self) -> dict[str, int]:
        """Return row counts as a mapping."""
        return {name: count for name, count in self.row_counts}

    def quality_mapping(self) -> dict[str, int]:
        """Return quality counters as a mapping."""
        return {name: count for name, count in self.quality_summary}

"""Read-only verification of immutable snapshots (sport-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.types import JsonValue, SnapshotRecord, validate_relative_snapshot_path
from sports_analytics.snapshots.manifest import (
    ValidatedManifest,
    load_manifest_bytes,
)
from sports_analytics.snapshots.parquet import file_sha256_and_size, verify_parquet_file
from sports_analytics.snapshots.paths import resolve_snapshot_dir, resolve_snapshot_file
from sports_analytics.snapshots.spec import MANIFEST_FILENAME, SnapshotDatasetSuite


@dataclass(frozen=True, slots=True)
class SnapshotVerificationResult:
    """Typed result of a successful read-only snapshot verification."""

    snapshot_id: str
    manifest_checksum_sha256: str
    relative_manifest_path: str
    manifest_version: str
    snapshot_type: str
    schema_version: str
    source_name: str
    source_version: str
    source_policy_version: str
    source_url: str
    raw_artifact_sha256: str
    partition_keys: tuple[tuple[str, str], ...]
    row_counts: tuple[tuple[str, int], ...]
    file_count: int
    byte_count: int
    primary_dataset_name: str
    primary_row_count: int
    quality_summary: tuple[tuple[str, int], ...]
    warnings_count: int
    source_observed_at_utc: datetime
    domain_metadata: dict[str, JsonValue]

    def row_count(self, dataset_name: str) -> int:
        """Return the verified row count for one dataset."""
        for name, count in self.row_counts:
            if name == dataset_name:
                return count
        msg = f"unknown snapshot dataset: {dataset_name}"
        raise SnapshotVerificationError(msg)


def resolve_manifest_path(snapshots_directory: Path, relative_manifest_path: str) -> Path:
    """Resolve a relative manifest path safely under the snapshots root."""
    validated = validate_relative_snapshot_path(relative_manifest_path)
    if not validated.endswith(f"/{MANIFEST_FILENAME}") and validated != MANIFEST_FILENAME:
        msg = "snapshot relative_path must point to manifest.json"
        raise SnapshotVerificationError(msg)
    return resolve_snapshot_file(snapshots_directory, validated)


def verify_snapshot_directory(
    *,
    snapshots_directory: Path,
    relative_manifest_path: str,
    suite: SnapshotDatasetSuite,
    expected_snapshot: SnapshotRecord | None = None,
) -> SnapshotVerificationResult:
    """Verify an immutable snapshot directory against its manifest and optional SQLite row."""
    manifest_path = resolve_manifest_path(snapshots_directory, relative_manifest_path)
    if not manifest_path.is_file():
        msg = "snapshot manifest is missing"
        raise SnapshotVerificationError(msg)
    manifest, _payload, digest = load_manifest_bytes(manifest_path, suite=suite)
    if expected_snapshot is not None:
        if expected_snapshot.checksum_sha256 != digest:
            msg = "manifest checksum does not match SnapshotRepository checksum"
            raise SnapshotVerificationError(msg)
        if expected_snapshot.id != manifest.snapshot_id:
            msg = "manifest snapshot_id does not match repository record"
            raise SnapshotVerificationError(msg)
        if expected_snapshot.snapshot_type != manifest.snapshot_type:
            msg = "manifest snapshot_type does not match repository record"
            raise SnapshotVerificationError(msg)
        if expected_snapshot.schema_version != manifest.schema_version:
            msg = "manifest schema_version does not match repository record"
            raise SnapshotVerificationError(msg)
        if expected_snapshot.source_name != manifest.source_name:
            msg = "manifest source_name does not match repository record"
            raise SnapshotVerificationError(msg)

    relative = validate_relative_snapshot_path(relative_manifest_path)
    relative_directory_path = PurePosixPath(relative).parent
    if relative_directory_path == PurePosixPath("."):
        directory = manifest_path.parent
        relative_directory = ""
    else:
        relative_directory = relative_directory_path.as_posix()
        if manifest.generated_snapshot_relative_path != relative_directory:
            msg = "manifest generated snapshot path does not match manifest location"
            raise SnapshotVerificationError(msg)
        directory = resolve_snapshot_dir(snapshots_directory, relative_directory)

    actual_files = {path.name for path in directory.iterdir()}
    expected_files = suite.expected_directory_files
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        msg = f"snapshot directory file set mismatch missing={missing} unexpected={unexpected}"
        raise SnapshotVerificationError(msg)

    byte_count = 0
    for descriptor in suite.descriptors:
        filename = descriptor.relative_filename
        file_relative_path = (
            PurePosixPath(relative_directory, filename).as_posix()
            if relative_directory
            else filename
        )
        path = resolve_snapshot_file(snapshots_directory, file_relative_path)
        meta = manifest.files_by_dataset[descriptor.dataset_name]
        digest_file, size = file_sha256_and_size(path)
        if digest_file != meta.sha256:
            msg = f"checksum mismatch for {filename}"
            raise SnapshotVerificationError(msg)
        if size != meta.byte_count:
            msg = f"byte count mismatch for {filename}"
            raise SnapshotVerificationError(msg)
        byte_count += size
        verify_parquet_file(
            path,
            expected_schema=descriptor.schema,
            expected_rows=meta.row_count,
        )

    primary_dataset = suite.primary_dataset_name
    primary_row_count = manifest.row_counts[primary_dataset]
    if expected_snapshot is not None and expected_snapshot.row_count != primary_row_count:
        msg = "repository row_count does not match primary dataset count"
        raise SnapshotVerificationError(msg)

    return SnapshotVerificationResult(
        snapshot_id=manifest.snapshot_id,
        manifest_checksum_sha256=digest,
        relative_manifest_path=relative,
        manifest_version=manifest.manifest_version,
        snapshot_type=manifest.snapshot_type,
        schema_version=manifest.schema_version,
        source_name=manifest.source_name,
        source_version=manifest.source_version,
        source_policy_version=manifest.source_policy_version,
        source_url=manifest.source_url,
        raw_artifact_sha256=manifest.raw_artifact.checksum_sha256,
        partition_keys=manifest.partition_keys,
        row_counts=tuple((name, manifest.row_counts[name]) for name in suite.dataset_names),
        file_count=len(suite.descriptors),
        byte_count=byte_count,
        primary_dataset_name=primary_dataset,
        primary_row_count=primary_row_count,
        quality_summary=tuple(
            (key, manifest.quality_summary[key]) for key in sorted(manifest.quality_summary)
        ),
        warnings_count=len(manifest.warnings),
        source_observed_at_utc=manifest.source_observed_at_utc,
        domain_metadata=dict(manifest.domain_metadata),
    )


def manifest_document_for(
    snapshots_directory: Path,
    relative_manifest_path: str,
    *,
    suite: SnapshotDatasetSuite,
) -> dict[str, JsonValue]:
    """Load a verified-path manifest document without mutating files."""
    path = resolve_manifest_path(snapshots_directory, relative_manifest_path)
    manifest, _, _ = load_manifest_bytes(path, suite=suite)
    return manifest.document


def repository_metadata(manifest: ValidatedManifest) -> dict[str, JsonValue]:
    """Build SQLite snapshot metadata from a validated manifest."""
    metadata: dict[str, JsonValue] = dict(manifest.domain_metadata)
    for key, value in manifest.partition_keys:
        metadata[key] = value
    metadata["source_policy_version"] = manifest.source_policy_version
    return metadata

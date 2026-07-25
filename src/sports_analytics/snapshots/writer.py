"""Prepare immutable snapshot directories on the filesystem (sport-agnostic)."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa

from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.data.types import JsonValue, normalize_uuid
from sports_analytics.snapshots.manifest import build_manifest_document, write_manifest
from sports_analytics.snapshots.parquet import write_suite_parquet_files
from sports_analytics.snapshots.paths import resolve_snapshot_dir
from sports_analytics.snapshots.spec import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    SnapshotDatasetSuite,
    SnapshotIdentity,
    SnapshotMetrics,
    SnapshotSpec,
)


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    """Filesystem-prepared snapshot awaiting short SQLite publication."""

    snapshot_id: str
    temporary_directory: Path
    relative_directory: str
    relative_manifest_path: str
    manifest_version: str
    identity: SnapshotIdentity
    suite: SnapshotDatasetSuite
    manifest_checksum_sha256: str
    metrics: SnapshotMetrics
    raw_artifact_sha256: str
    source_observed_at_utc: datetime
    domain_metadata: dict[str, JsonValue]

    @property
    def snapshot_type(self) -> str:
        """Return the snapshot type from the validated identity."""
        return self.identity.snapshot_type

    @property
    def schema_version(self) -> str:
        """Return the schema version from the validated identity."""
        return self.identity.schema_version

    @property
    def source_name(self) -> str:
        """Return the source name from the validated identity."""
        return self.identity.source_name

    @property
    def source_version(self) -> str:
        """Return the source version from the validated identity."""
        return self.identity.source_version

    @property
    def partition_keys(self) -> tuple[tuple[str, str], ...]:
        """Return ordered partition keys from the validated identity."""
        return self.identity.partition_keys

    @property
    def primary_row_count(self) -> int:
        """Return the row count stored as the SQLite snapshot ``row_count``."""
        return self.metrics.row_count(self.suite.primary_dataset_name)


def prepare_snapshot_directory(
    *,
    snapshots_directory: Path,
    spec: SnapshotSpec,
    tables: dict[str, pa.Table],
    snapshot_id: str | uuid.UUID | None = None,
) -> PreparedSnapshot:
    """Write Parquet files and a manifest into a temporary directory under snapshots."""
    normalized_id = normalize_uuid(snapshot_id)
    relative_directory = spec.identity.relative_directory(normalized_id)
    relative_manifest = spec.identity.relative_manifest_path(normalized_id)

    snapshots_root = Path(snapshots_directory).resolve()
    snapshots_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".snap-{normalized_id}-",
            dir=str(snapshots_root),
        )
    )
    try:
        file_meta = write_suite_parquet_files(temp_dir, suite=spec.suite, tables=tables)
        document = build_manifest_document(
            snapshot_id=normalized_id,
            spec=spec,
            file_meta=file_meta,
            snapshot_relative_directory=relative_directory,
        )
        _, manifest_checksum = write_manifest(temp_dir / MANIFEST_FILENAME, document)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    row_counts = tuple(
        (name, int(file_meta[name]["row_count"]))  # type: ignore[call-overload]
        for name in spec.suite.dataset_names
    )
    byte_count = sum(
        int(file_meta[name]["byte_count"])  # type: ignore[call-overload]
        for name in spec.suite.dataset_names
    )
    metrics = SnapshotMetrics(
        row_counts=row_counts,
        file_count=len(spec.suite.descriptors),
        byte_count=byte_count,
        quality_summary=tuple(
            (key, spec.quality_summary[key]) for key in sorted(spec.quality_summary)
        ),
        warnings_count=len(spec.warnings),
    )
    return PreparedSnapshot(
        snapshot_id=normalized_id,
        temporary_directory=temp_dir,
        relative_directory=relative_directory,
        relative_manifest_path=relative_manifest,
        manifest_version=MANIFEST_VERSION,
        identity=spec.identity,
        suite=spec.suite,
        manifest_checksum_sha256=manifest_checksum,
        metrics=metrics,
        raw_artifact_sha256=spec.raw_artifact.checksum_sha256,
        source_observed_at_utc=spec.source_observed_at_utc,
        domain_metadata=dict(spec.domain_metadata),
    )


def discard_prepared_snapshot(prepared: PreparedSnapshot) -> None:
    """Best-effort removal of a temporary prepared snapshot directory."""
    try:
        shutil.rmtree(prepared.temporary_directory, ignore_errors=True)
    except OSError:
        pass


def resolve_snapshot_directory(snapshots_directory: Path, relative_directory: str) -> Path:
    """Resolve a relative snapshot directory safely under the snapshots root."""
    try:
        return resolve_snapshot_dir(snapshots_directory, relative_directory)
    except SnapshotVerificationError as exc:
        msg = "invalid snapshot directory path"
        raise SnapshotIntegrityError(msg) from exc

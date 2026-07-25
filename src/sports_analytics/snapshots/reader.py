"""Read-only verification of immutable football snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.types import JsonValue, SnapshotRecord, validate_relative_snapshot_path
from sports_analytics.snapshots.manifest import (
    expected_parquet_filenames,
    load_manifest_bytes,
)
from sports_analytics.snapshots.parquet import file_sha256_and_size, verify_parquet_file
from sports_analytics.sports.football.contracts import (
    CANONICAL_DATASETS,
    MANIFEST_FILENAME,
    PARQUET_FILENAMES,
)
from sports_analytics.sports.football.schemas import dataset_schema


@dataclass(frozen=True, slots=True)
class SnapshotVerificationResult:
    """Typed result of a successful read-only snapshot verification."""

    snapshot_id: str
    manifest_checksum_sha256: str
    games_count: int
    file_count: int
    relative_manifest_path: str


def resolve_manifest_path(snapshots_directory: Path, relative_manifest_path: str) -> Path:
    """Resolve a relative manifest path safely under the snapshots root."""
    validated = validate_relative_snapshot_path(relative_manifest_path)
    if not validated.endswith(f"/{MANIFEST_FILENAME}") and validated != MANIFEST_FILENAME:
        msg = "snapshot relative_path must point to manifest.json"
        raise SnapshotVerificationError(msg)
    root = Path(snapshots_directory).resolve()
    candidate = (root / Path(*validated.split("/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        msg = "manifest path escapes configured snapshots directory"
        raise SnapshotVerificationError(msg) from exc
    if candidate.is_symlink():
        msg = "manifest path must not be a symlink"
        raise SnapshotVerificationError(msg)
    return candidate


def verify_snapshot_directory(
    *,
    snapshots_directory: Path,
    relative_manifest_path: str,
    expected_snapshot: SnapshotRecord | None = None,
) -> SnapshotVerificationResult:
    """Verify an immutable snapshot directory against its manifest and optional SQLite row."""
    manifest_path = resolve_manifest_path(snapshots_directory, relative_manifest_path)
    if not manifest_path.is_file():
        msg = "snapshot manifest is missing"
        raise SnapshotVerificationError(msg)
    document, _payload, digest = load_manifest_bytes(manifest_path)
    if expected_snapshot is not None:
        if expected_snapshot.checksum_sha256 != digest:
            msg = "manifest checksum does not match SnapshotRepository checksum"
            raise SnapshotVerificationError(msg)
        if expected_snapshot.id != document.get("snapshot_id"):
            msg = "manifest snapshot_id does not match repository record"
            raise SnapshotVerificationError(msg)
    directory = manifest_path.parent
    expected_files = expected_parquet_filenames() | {MANIFEST_FILENAME}
    actual_files = {path.name for path in directory.iterdir()}
    # Reject unexpected files; ignore nothing in final directories.
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        msg = f"snapshot directory file set mismatch missing={missing} unexpected={unexpected}"
        raise SnapshotVerificationError(msg)

    files_meta = document.get("files")
    if not isinstance(files_meta, list):
        msg = "manifest files must be a list"
        raise SnapshotVerificationError(msg)
    by_name = {
        str(item["relative_filename"]): item
        for item in files_meta
        if isinstance(item, dict) and "relative_filename" in item
    }
    row_counts = document.get("row_counts")
    if not isinstance(row_counts, dict):
        msg = "manifest row_counts must be an object"
        raise SnapshotVerificationError(msg)

    for dataset_name in CANONICAL_DATASETS:
        filename = PARQUET_FILENAMES[dataset_name]
        path = directory / filename
        meta = by_name.get(filename)
        if meta is None or not isinstance(meta, dict):
            msg = f"manifest missing file entry for {filename}"
            raise SnapshotVerificationError(msg)
        digest_file, size = file_sha256_and_size(path)
        if digest_file != meta.get("sha256"):
            msg = f"checksum mismatch for {filename}"
            raise SnapshotVerificationError(msg)
        if size != meta.get("byte_count"):
            msg = f"byte count mismatch for {filename}"
            raise SnapshotVerificationError(msg)
        expected_rows = int(meta["row_count"])  # type: ignore[arg-type]
        if row_counts.get(dataset_name) != expected_rows:
            msg = f"row_counts mismatch for {dataset_name}"
            raise SnapshotVerificationError(msg)
        verify_parquet_file(
            path,
            expected_schema=dataset_schema(dataset_name),
            expected_rows=expected_rows,
        )

    games_count = int(row_counts["games"])  # type: ignore[arg-type]
    if expected_snapshot is not None and expected_snapshot.row_count != games_count:
        msg = "repository row_count does not match games count"
        raise SnapshotVerificationError(msg)

    relative = validate_relative_snapshot_path(
        PurePosixPath(*relative_manifest_path.split("/")).as_posix()
        if "/" in relative_manifest_path
        else relative_manifest_path
    )
    return SnapshotVerificationResult(
        snapshot_id=str(document["snapshot_id"]),
        manifest_checksum_sha256=digest,
        games_count=games_count,
        file_count=len(expected_parquet_filenames()),
        relative_manifest_path=relative,
    )


def manifest_document_for(
    snapshots_directory: Path,
    relative_manifest_path: str,
) -> dict[str, JsonValue]:
    """Load a verified-path manifest document without mutating files."""
    path = resolve_manifest_path(snapshots_directory, relative_manifest_path)
    document, _, _ = load_manifest_bytes(path)
    return document

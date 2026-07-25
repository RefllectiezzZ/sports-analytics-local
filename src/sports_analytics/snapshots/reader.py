"""Read-only verification of immutable football snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.types import JsonValue, SnapshotRecord, validate_relative_snapshot_path
from sports_analytics.snapshots.manifest import (
    ValidatedManifest,
    expected_parquet_filenames,
    load_manifest_bytes,
)
from sports_analytics.snapshots.parquet import file_sha256_and_size, verify_parquet_file
from sports_analytics.snapshots.paths import resolve_snapshot_dir, resolve_snapshot_file
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
    manifest_version: str | None = None
    snapshot_type: str | None = None
    source_name: str | None = None
    source_version: str | None = None
    schema_version: str | None = None
    competition_id: str | None = None
    season_id: str | None = None
    source_file_sha256: str | None = None
    source_competition_code: str | None = None
    source_season_code: str | None = None
    teams_count: int | None = None
    odds_quotes_count: int | None = None
    statistics_rows_count: int | None = None
    duplicate_rows_discarded: int | None = None
    warnings_count: int | None = None
    source_observed_at_utc: datetime | None = None
    metadata: dict[str, JsonValue] | None = None


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
    expected_snapshot: SnapshotRecord | None = None,
) -> SnapshotVerificationResult:
    """Verify an immutable snapshot directory against its manifest and optional SQLite row."""
    manifest_path = resolve_manifest_path(snapshots_directory, relative_manifest_path)
    if not manifest_path.is_file():
        msg = "snapshot manifest is missing"
        raise SnapshotVerificationError(msg)
    manifest, _payload, digest = load_manifest_bytes(manifest_path)
    if expected_snapshot is not None:
        if expected_snapshot.checksum_sha256 != digest:
            msg = "manifest checksum does not match SnapshotRepository checksum"
            raise SnapshotVerificationError(msg)
        if expected_snapshot.id != manifest.snapshot_id:
            msg = "manifest snapshot_id does not match repository record"
            raise SnapshotVerificationError(msg)

    relative = validate_relative_snapshot_path(
        PurePosixPath(*relative_manifest_path.split("/")).as_posix()
        if "/" in relative_manifest_path
        else relative_manifest_path
    )
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

    expected_files = expected_parquet_filenames() | {MANIFEST_FILENAME}
    actual_files = {path.name for path in directory.iterdir()}
    # Reject unexpected files; ignore nothing in final directories.
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        msg = f"snapshot directory file set mismatch missing={missing} unexpected={unexpected}"
        raise SnapshotVerificationError(msg)

    for dataset_name in CANONICAL_DATASETS:
        filename = PARQUET_FILENAMES[dataset_name]
        file_relative_path = (
            PurePosixPath(relative_directory, filename).as_posix()
            if relative_directory
            else filename
        )
        path = resolve_snapshot_file(snapshots_directory, file_relative_path)
        meta = manifest.files_by_dataset[dataset_name]
        digest_file, size = file_sha256_and_size(path)
        if digest_file != meta.sha256:
            msg = f"checksum mismatch for {filename}"
            raise SnapshotVerificationError(msg)
        if size != meta.byte_count:
            msg = f"byte count mismatch for {filename}"
            raise SnapshotVerificationError(msg)
        expected_rows = meta.row_count
        verify_parquet_file(
            path,
            expected_schema=dataset_schema(dataset_name),
            expected_rows=expected_rows,
        )

    games_count = manifest.games_count
    if expected_snapshot is not None and expected_snapshot.row_count != games_count:
        msg = "repository row_count does not match games count"
        raise SnapshotVerificationError(msg)

    return SnapshotVerificationResult(
        snapshot_id=manifest.snapshot_id,
        manifest_checksum_sha256=digest,
        games_count=games_count,
        file_count=len(expected_parquet_filenames()),
        relative_manifest_path=relative,
        manifest_version=manifest.manifest_version,
        snapshot_type=manifest.snapshot_type,
        source_name=manifest.source_name,
        source_version=manifest.source_version,
        schema_version=manifest.schema_version,
        competition_id=manifest.competition_id,
        season_id=manifest.season_id,
        source_file_sha256=manifest.raw_artifact_checksum_sha256,
        source_competition_code=manifest.source_competition_code,
        source_season_code=manifest.source_season_code,
        teams_count=manifest.teams_count,
        odds_quotes_count=manifest.odds_quotes_count,
        statistics_rows_count=manifest.statistics_rows_count,
        duplicate_rows_discarded=manifest.duplicate_source_rows_discarded,
        warnings_count=manifest.quality_summary.warnings_count,
        source_observed_at_utc=manifest.source_observed_at_utc,
        metadata=_repository_metadata(manifest),
    )


def manifest_document_for(
    snapshots_directory: Path,
    relative_manifest_path: str,
) -> dict[str, JsonValue]:
    """Load a verified-path manifest document without mutating files."""
    path = resolve_manifest_path(snapshots_directory, relative_manifest_path)
    manifest, _, _ = load_manifest_bytes(path)
    return manifest.document


def _repository_metadata(manifest: ValidatedManifest) -> dict[str, JsonValue]:
    return {
        "competition_id": manifest.competition_id,
        "season_id": manifest.season_id,
        "source_competition_code": manifest.source_competition_code,
        "source_season_code": manifest.source_season_code,
        "source_file_sha256": manifest.raw_artifact_checksum_sha256,
        "games_count": manifest.games_count,
        "teams_count": manifest.teams_count,
        "odds_quotes_count": manifest.odds_quotes_count,
        "statistics_rows_count": manifest.statistics_rows_count,
        "duplicate_rows_discarded": manifest.duplicate_source_rows_discarded,
        "warnings_count": manifest.quality_summary.warnings_count,
    }

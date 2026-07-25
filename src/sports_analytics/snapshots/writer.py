"""Prepare immutable football snapshot directories on the filesystem."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.data.types import JsonValue, normalize_uuid, validate_relative_snapshot_path
from sports_analytics.snapshots.manifest import build_manifest_document, write_manifest
from sports_analytics.snapshots.parquet import write_bundle_parquet_files
from sports_analytics.snapshots.paths import resolve_snapshot_dir
from sports_analytics.sources.raw_store import RawSourceArtifact
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
)
from sports_analytics.sports.football.identifiers import build_season_id, build_source_version
from sports_analytics.sports.football.normalization import NormalizedFootballBundle


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    """Filesystem-prepared snapshot awaiting short SQLite publication."""

    snapshot_id: str
    temporary_directory: Path
    relative_directory: str
    relative_manifest_path: str
    source_version: str
    source_name: str
    schema_version: str
    snapshot_type: str
    manifest_version: str
    competition_id: str
    season_id: str
    source_competition_code: str
    source_season_code: str
    manifest_checksum_sha256: str
    games_count: int
    teams_count: int
    odds_quotes_count: int
    statistics_rows_count: int
    duplicate_rows_discarded: int
    warnings_count: int
    source_file_sha256: str
    source_observed_at_utc: datetime
    metadata: dict[str, JsonValue]


def build_relative_snapshot_directory(
    *,
    competition_id: str,
    season_label: str,
    snapshot_id: str,
) -> str:
    """Return the relative snapshot directory under the snapshots root."""
    relative = PurePosixPath(
        FOOTBALL_INGESTION_SNAPSHOT_TYPE,
        FOOTBALL_CANONICAL_SCHEMA_VERSION,
        competition_id,
        season_label,
        snapshot_id,
    ).as_posix()
    return validate_relative_snapshot_path(relative)


def prepare_snapshot_directory(
    *,
    snapshots_directory: Path,
    bundle: NormalizedFootballBundle,
    artifact: RawSourceArtifact,
    competition_id: str,
    season_label: str,
    source_competition_code: str,
    source_season_code: str,
    source_url: str,
    source_observed_at_utc: datetime,
    unknown_source_columns: tuple[str, ...],
    missing_optional_source_columns: tuple[str, ...],
    http_status: int | None = None,
    http_content_type: str | None = None,
    http_content_length: int | None = None,
    http_etag: str | None = None,
    http_last_modified: str | None = None,
    http_final_url: str | None = None,
    snapshot_id: str | uuid.UUID | None = None,
) -> PreparedSnapshot:
    """Write Parquet files and manifest into a temporary directory under snapshots."""
    normalized_id = normalize_uuid(snapshot_id)
    season_id = build_season_id(competition_id=competition_id, label=season_label)
    source_version = build_source_version(
        source_competition_code=source_competition_code,
        source_season_code=source_season_code,
        raw_sha256=artifact.checksum_sha256,
    )
    relative_directory = build_relative_snapshot_directory(
        competition_id=competition_id,
        season_label=season_label,
        snapshot_id=normalized_id,
    )
    relative_manifest = validate_relative_snapshot_path(
        PurePosixPath(relative_directory, MANIFEST_FILENAME).as_posix()
    )
    snapshots_root = Path(snapshots_directory).resolve()
    snapshots_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".snap-{normalized_id}-",
            dir=str(snapshots_root),
        )
    )
    try:
        file_meta = write_bundle_parquet_files(temp_dir, bundle)
        document = build_manifest_document(
            snapshot_id=normalized_id,
            source_name=artifact.source_name,
            source_version=source_version,
            source_competition_code=source_competition_code,
            source_season_code=source_season_code,
            competition_id=competition_id,
            season_id=season_id,
            source_url=source_url,
            source_observed_at_utc=source_observed_at_utc,
            raw_relative_path=artifact.relative_path,
            raw_checksum_sha256=artifact.checksum_sha256,
            raw_bytes=artifact.byte_count,
            raw_encoding=artifact.encoding,
            http_status=http_status,
            http_content_type=http_content_type,
            http_content_length=http_content_length,
            http_etag=http_etag,
            http_last_modified=http_last_modified,
            http_final_url=http_final_url,
            bundle=bundle,
            file_meta=file_meta,
            snapshot_relative_directory=relative_directory,
            unknown_source_columns=unknown_source_columns,
            missing_optional_source_columns=missing_optional_source_columns,
        )
        _, manifest_checksum = write_manifest(temp_dir / MANIFEST_FILENAME, document)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    metadata: dict[str, JsonValue] = {
        "competition_id": competition_id,
        "season_id": season_id,
        "source_competition_code": source_competition_code,
        "source_season_code": source_season_code,
        "games_count": len(bundle.games),
        "teams_count": len(bundle.teams),
        "odds_quotes_count": len(bundle.odds_1x2),
        "statistics_rows_count": len(bundle.post_match_statistics),
    }
    return PreparedSnapshot(
        snapshot_id=normalized_id,
        temporary_directory=temp_dir,
        relative_directory=relative_directory,
        relative_manifest_path=relative_manifest,
        source_version=source_version,
        source_name=artifact.source_name,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
        manifest_version=MANIFEST_VERSION,
        competition_id=competition_id,
        season_id=season_id,
        source_competition_code=source_competition_code,
        source_season_code=source_season_code,
        manifest_checksum_sha256=manifest_checksum,
        games_count=len(bundle.games),
        teams_count=len(bundle.teams),
        odds_quotes_count=len(bundle.odds_1x2),
        statistics_rows_count=len(bundle.post_match_statistics),
        duplicate_rows_discarded=bundle.duplicate_rows_discarded,
        warnings_count=len(bundle.warnings),
        source_file_sha256=artifact.checksum_sha256,
        source_observed_at_utc=source_observed_at_utc,
        metadata=metadata,
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

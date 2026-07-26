"""Strict bookmaker snapshot loader with semantic verification."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sports_analytics.bookmakers.schemas import bookmaker_snapshot_suite
from sports_analytics.bookmakers.types import BOOKMAKER_SCHEMA_VERSION, BOOKMAKER_SNAPSHOT_TYPE
from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sources.bookmaker_capture import verify_capture_manifest


@dataclass(frozen=True, slots=True)
class LoadedBookmakerSnapshot:
    """Verified bookmaker snapshot with registration agreement."""

    snapshot_id: str
    provider_id: str
    sport: str
    schema_version: str
    relative_path: str
    checksum_sha256: str
    verified: bool
    registration_only: bool = False


def load_bookmaker_snapshot(
    *,
    database_connection: sqlite3.Connection,
    snapshots_directory: Path,
    raw_directory: Path,
    snapshot_id: str,
) -> LoadedBookmakerSnapshot:
    """Verify generic snapshot, bookmaker registration, datasets, and capture manifest."""
    repo = BookmakerRepository(database_connection)
    snapshots = SnapshotRepository(database_connection)
    record = snapshots.get_snapshot(snapshot_id)
    registration = repo.get_snapshot_registration(snapshot_id)

    if record is None and registration is None:
        raise SnapshotVerificationError(f"bookmaker snapshot not found: {snapshot_id}")

    if record is None:
        raise SnapshotVerificationError(
            f"snapshot {snapshot_id} has bookmaker registration only; generic record missing"
        )

    if registration is None:
        raise SnapshotVerificationError(
            f"snapshot {snapshot_id} has generic record only; bookmaker registration missing"
        )

    if str(registration["snapshot_id"]) != snapshot_id:
        raise SnapshotVerificationError("registration snapshot_id mismatch")
    if str(registration["relative_path"]) != record.relative_path:
        raise SnapshotVerificationError("registration relative_path mismatch")
    if str(registration["checksum_sha256"]) != record.checksum_sha256:
        raise SnapshotVerificationError("registration checksum mismatch")
    if str(registration["schema_version"]) != record.schema_version:
        raise SnapshotVerificationError("registration schema_version mismatch")

    if record.status is not SnapshotStatus.READY:
        raise SnapshotVerificationError(
            f"snapshot {snapshot_id} is not READY (status={record.status.value})"
        )
    if record.snapshot_type != BOOKMAKER_SNAPSHOT_TYPE:
        raise SnapshotVerificationError(
            f"snapshot {snapshot_id} has unexpected type {record.snapshot_type!r}"
        )
    if record.schema_version != BOOKMAKER_SCHEMA_VERSION:
        raise SnapshotVerificationError(
            f"snapshot {snapshot_id} has unexpected schema {record.schema_version!r}"
        )

    sport_code = str(registration["sport"])
    result = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=record.relative_path,
        suite=bookmaker_snapshot_suite(sport_code=sport_code),
        expected_snapshot=record,
    )

    capture_manifest_path = result.domain_metadata.get("capture_manifest_relative_path")
    capture_manifest_checksum = result.domain_metadata.get("capture_manifest_checksum_sha256")
    if isinstance(capture_manifest_path, str) and capture_manifest_path:
        from sports_analytics.sources.bookmaker_capture import CaptureManifest

        manifest_abs = raw_directory / capture_manifest_path
        if not manifest_abs.is_file():
            raise SnapshotVerificationError("capture manifest file missing")
        manifest_bytes = manifest_abs.read_bytes()
        import hashlib

        checksum = hashlib.sha256(manifest_bytes).hexdigest()
        if isinstance(capture_manifest_checksum, str) and checksum != capture_manifest_checksum:
            raise SnapshotVerificationError("capture manifest checksum mismatch with snapshot")
        manifest = CaptureManifest(
            schema="bookmaker-capture-manifest-v1",
            provider_id=str(registration["provider_id"]),
            acquisition_cycle_id=str(registration["acquisition_cycle_id"]),
            entries=(),
            manifest_bytes=manifest_bytes,
            checksum_sha256=checksum,
            relative_path=capture_manifest_path,
        )
        verify_capture_manifest(raw_directory=raw_directory, manifest=manifest)

    return LoadedBookmakerSnapshot(
        snapshot_id=snapshot_id,
        provider_id=str(registration["provider_id"]),
        sport=sport_code,
        schema_version=result.schema_version,
        relative_path=record.relative_path,
        checksum_sha256=result.manifest_checksum_sha256,
        verified=True,
        registration_only=False,
    )

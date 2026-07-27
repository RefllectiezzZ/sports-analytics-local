"""Strict bookmaker snapshot loader with semantic verification."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from sports_analytics.bookmakers.schemas import (
    DATASET_CANONICAL_EVENTS,
    DATASET_COMPARISON_ELIGIBILITY,
    bookmaker_snapshot_suite,
)
from sports_analytics.bookmakers.types import BOOKMAKER_SCHEMA_VERSION, BOOKMAKER_SNAPSHOT_TYPE
from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.markets.schemas import DATASET_MARKET_QUOTES
from sports_analytics.snapshots.paths import resolve_raw_path, resolve_snapshot_dir
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sources.bookmaker_capture import (
    parse_capture_manifest_from_bytes,
    verify_capture_manifest,
)


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
    event_count: int = 0
    quote_count: int = 0


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
    provider_id = str(registration["provider_id"])
    acquisition_cycle_id = str(registration["acquisition_cycle_id"])
    result = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=record.relative_path,
        suite=bookmaker_snapshot_suite(sport_code=sport_code),
        expected_snapshot=record,
    )

    capture_manifest_path = result.domain_metadata.get("capture_manifest_relative_path")
    capture_manifest_checksum = result.domain_metadata.get("capture_manifest_checksum_sha256")
    if not isinstance(capture_manifest_path, str) or not capture_manifest_path.strip():
        raise SnapshotVerificationError("capture manifest path metadata is required")
    if not isinstance(capture_manifest_checksum, str) or not capture_manifest_checksum.strip():
        raise SnapshotVerificationError("capture manifest checksum metadata is required")

    raw_root = Path(raw_directory).resolve()
    if raw_root.is_symlink():
        raise SnapshotVerificationError("configured raw directory must not be a symlink")
    manifest_abs = resolve_raw_path(raw_root, capture_manifest_path)
    if manifest_abs.is_symlink():
        raise SnapshotVerificationError("capture manifest path must not be a symlink")
    if not manifest_abs.is_file():
        raise SnapshotVerificationError("capture manifest file missing")
    manifest_bytes = manifest_abs.read_bytes()
    manifest = parse_capture_manifest_from_bytes(
        manifest_bytes=manifest_bytes,
        relative_path=capture_manifest_path,
        expected_provider_id=provider_id,
        expected_acquisition_cycle_id=acquisition_cycle_id,
    )
    if manifest.checksum_sha256 != capture_manifest_checksum:
        raise SnapshotVerificationError("capture manifest checksum mismatch with snapshot metadata")
    if manifest.relative_path != capture_manifest_path:
        raise SnapshotVerificationError("capture manifest relative path mismatch")
    if result.raw_artifact_sha256 != manifest.checksum_sha256:
        raise SnapshotVerificationError("snapshot raw artifact checksum mismatch with manifest")
    if acquisition_cycle_id not in result.source_version:
        raise SnapshotVerificationError("source_version must reference acquisition cycle id")
    verify_capture_manifest(raw_directory=raw_root, manifest=manifest)

    from pathlib import PurePosixPath

    manifest_parent = PurePosixPath(record.relative_path).parent.as_posix()
    snapshot_dir = resolve_snapshot_dir(snapshots_directory, manifest_parent)
    event_count, quote_count = _verify_semantic_datasets(
        snapshot_dir=snapshot_dir,
        sport_code=sport_code,
        provider_id=provider_id,
        acquisition_cycle_id=acquisition_cycle_id,
        expected_event_rows=result.row_count(DATASET_CANONICAL_EVENTS),
        expected_quote_rows=result.row_count(DATASET_MARKET_QUOTES),
    )

    return LoadedBookmakerSnapshot(
        snapshot_id=snapshot_id,
        provider_id=provider_id,
        sport=sport_code,
        schema_version=result.schema_version,
        relative_path=record.relative_path,
        checksum_sha256=result.manifest_checksum_sha256,
        verified=True,
        registration_only=False,
        event_count=event_count,
        quote_count=quote_count,
    )


def _verify_semantic_datasets(
    *,
    snapshot_dir: Path,
    sport_code: str,
    provider_id: str,
    acquisition_cycle_id: str,
    expected_event_rows: int,
    expected_quote_rows: int,
) -> tuple[int, int]:
    events_path = snapshot_dir / f"{DATASET_CANONICAL_EVENTS}.parquet"
    quotes_path = snapshot_dir / f"{DATASET_MARKET_QUOTES}.parquet"
    eligibility_path = snapshot_dir / f"{DATASET_COMPARISON_ELIGIBILITY}.parquet"
    for path in (events_path, quotes_path, eligibility_path):
        if path.is_symlink():
            raise SnapshotVerificationError(f"dataset must not be a symlink: {path.name}")
        if not path.is_file():
            raise SnapshotVerificationError(f"required dataset missing: {path.name}")
    events = pq.read_table(events_path)
    quotes = pq.read_table(quotes_path)
    eligibility = pq.read_table(eligibility_path)
    if events.num_rows != expected_event_rows:
        raise SnapshotVerificationError("canonical event row count mismatch")
    if quotes.num_rows != expected_quote_rows:
        raise SnapshotVerificationError("market quote row count mismatch")
    if events.num_rows < 1 or quotes.num_rows < 1:
        raise SnapshotVerificationError("admitted snapshot must contain events and quotes")
    event_ids = {str(value) for value in events.column("canonical_event_id").to_pylist()}
    if len(event_ids) != events.num_rows:
        raise SnapshotVerificationError("canonical event identities must be unique")
    quote_event_ids = quotes.column("canonical_event_id").to_pylist()
    for event_id in quote_event_ids:
        if str(event_id) not in event_ids:
            raise SnapshotVerificationError("quote references unresolved canonical event")
    market_keys = quotes.column("market_key").to_pylist()
    outcome_keys = quotes.column("outcome_key").to_pylist()
    if len(set(zip(market_keys, outcome_keys, strict=True))) != quotes.num_rows:
        raise SnapshotVerificationError("canonical market/selection identities must be unique")
    if "provider_id" in quotes.schema.names:
        for observed_provider in quotes.column("provider_id").to_pylist():
            if str(observed_provider) != provider_id:
                raise SnapshotVerificationError("quote provider_id mismatch with registration")
    if "acquisition_cycle_id" in eligibility.schema.names:
        for cycle in eligibility.column("acquisition_cycle_id").to_pylist():
            if str(cycle) != acquisition_cycle_id:
                raise SnapshotVerificationError("eligibility acquisition_cycle_id mismatch")
    if "sport_code" in events.schema.names:
        for sport in events.column("sport_code").to_pylist():
            if str(sport) != sport_code:
                raise SnapshotVerificationError("event sport mismatch with registration")
    return events.num_rows, quotes.num_rows

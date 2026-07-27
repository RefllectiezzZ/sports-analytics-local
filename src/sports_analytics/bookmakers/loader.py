"""Strict bookmaker snapshot loader with semantic verification."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from sports_analytics.bookmakers.schemas import (
    DATASET_ACQUISITION_METADATA,
    DATASET_CANONICAL_EVENTS,
    DATASET_COMPARISON_ELIGIBILITY,
    DATASET_PARSER_DRIFT_FINDINGS,
    DATASET_PROVIDER_STATUS,
    bookmaker_snapshot_suite,
)
from sports_analytics.bookmakers.snapshots import parse_bookmaker_source_version
from sports_analytics.bookmakers.types import BOOKMAKER_SCHEMA_VERSION, BOOKMAKER_SNAPSHOT_TYPE
from sports_analytics.bookmakers.verified_evidence import (
    quote_semantic_identity_key,
    verify_quote_row_identity,
)
from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.markets.schemas import DATASET_MARKET_QUOTES
from sports_analytics.snapshots.paths import resolve_raw_path, resolve_snapshot_dir
from sports_analytics.snapshots.reader import SnapshotVerificationResult, verify_snapshot_directory
from sports_analytics.sources.bookmaker_capture import (
    parse_capture_manifest_from_bytes,
    verify_capture_manifest,
)
from sports_analytics.sports.contracts import ReconciliationState
from sports_analytics.sports.schemas import (
    DATASET_EVENT_RECONCILIATIONS,
    DATASET_PARTICIPANT_RECONCILIATIONS,
    DATASET_SOURCE_EVENTS,
    DATASET_SOURCE_PARTICIPANTS,
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

    parsed_source = parse_bookmaker_source_version(result.source_version)
    if parsed_source.sport_code != sport_code:
        raise SnapshotVerificationError("source_version sport does not match registration")
    if parsed_source.acquisition_cycle_id != acquisition_cycle_id:
        raise SnapshotVerificationError(
            "source_version acquisition_cycle_id does not match registration"
        )
    if parsed_source.raw_sha256 != result.raw_artifact_sha256:
        raise SnapshotVerificationError("source_version raw checksum mismatch with manifest")

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
    verify_capture_manifest(raw_directory=raw_root, manifest=manifest)

    from pathlib import PurePosixPath

    manifest_parent = PurePosixPath(record.relative_path).parent.as_posix()
    snapshot_dir = resolve_snapshot_dir(snapshots_directory, manifest_parent)
    event_count, quote_count = _verify_semantic_datasets(
        snapshot_dir=snapshot_dir,
        verification=result,
        sport_code=sport_code,
        provider_id=provider_id,
        acquisition_cycle_id=acquisition_cycle_id,
        capture_checksums={entry.checksum_sha256 for entry in manifest.entries},
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


def _read_dataset(snapshot_dir: Path, dataset_name: str) -> list[dict[str, Any]]:
    path = snapshot_dir / f"{dataset_name}.parquet"
    if path.is_symlink():
        raise SnapshotVerificationError(f"dataset must not be a symlink: {dataset_name}")
    if not path.is_file():
        raise SnapshotVerificationError(f"required dataset missing: {dataset_name}")
    table = pq.read_table(path)
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows


def _verify_semantic_datasets(
    *,
    snapshot_dir: Path,
    verification: SnapshotVerificationResult,
    sport_code: str,
    provider_id: str,
    acquisition_cycle_id: str,
    capture_checksums: set[str],
) -> tuple[int, int]:
    suite = bookmaker_snapshot_suite(sport_code=sport_code)
    for descriptor in suite.descriptors:
        expected_rows = verification.row_count(descriptor.dataset_name)
        rows = _read_dataset(snapshot_dir, descriptor.dataset_name)
        if len(rows) != expected_rows:
            raise SnapshotVerificationError(
                f"{descriptor.dataset_name} row count mismatch with manifest"
            )

    acquisition_rows = _read_dataset(snapshot_dir, DATASET_ACQUISITION_METADATA)
    provider_status_rows = _read_dataset(snapshot_dir, DATASET_PROVIDER_STATUS)
    source_participants = _read_dataset(snapshot_dir, DATASET_SOURCE_PARTICIPANTS)
    participant_reconciliations = _read_dataset(snapshot_dir, DATASET_PARTICIPANT_RECONCILIATIONS)
    source_events = _read_dataset(snapshot_dir, DATASET_SOURCE_EVENTS)
    event_reconciliations = _read_dataset(snapshot_dir, DATASET_EVENT_RECONCILIATIONS)
    events = _read_dataset(snapshot_dir, DATASET_CANONICAL_EVENTS)
    quotes = _read_dataset(snapshot_dir, DATASET_MARKET_QUOTES)
    drift_findings = _read_dataset(snapshot_dir, DATASET_PARSER_DRIFT_FINDINGS)
    eligibility = _read_dataset(snapshot_dir, DATASET_COMPARISON_ELIGIBILITY)

    if len(acquisition_rows) != 1:
        raise SnapshotVerificationError("acquisition metadata must contain exactly one row")
    acquisition = acquisition_rows[0]
    if str(acquisition.get("provider_id")) != provider_id:
        raise SnapshotVerificationError("acquisition metadata provider_id mismatch")
    if str(acquisition.get("sport")) != sport_code:
        raise SnapshotVerificationError("acquisition metadata sport mismatch")
    if str(acquisition.get("acquisition_cycle_id")) != acquisition_cycle_id:
        raise SnapshotVerificationError("acquisition metadata acquisition_cycle_id mismatch")
    if int(acquisition.get("event_count", -1)) != len(events):
        raise SnapshotVerificationError("acquisition metadata event_count mismatch")

    provider_status_for_provider = [
        row for row in provider_status_rows if str(row.get("provider_id")) == provider_id
    ]
    if provider_status_rows:
        if len(provider_status_for_provider) != 1:
            raise SnapshotVerificationError("provider status must contain exactly one provider row")
        provider_status = provider_status_for_provider[0]
        if int(provider_status.get("valid_quotes_observed", -1)) != len(quotes):
            raise SnapshotVerificationError("provider status valid_quotes_observed mismatch")

    _verify_source_participant_graph(
        source_participants=source_participants,
        participant_reconciliations=participant_reconciliations,
        sport_code=sport_code,
    )
    resolved_event_ids = _verify_source_event_graph(
        source_events=source_events,
        event_reconciliations=event_reconciliations,
        sport_code=sport_code,
    )

    if len(events) < 1 or len(quotes) < 1:
        raise SnapshotVerificationError("admitted snapshot must contain events and quotes")
    event_ids = {str(row["canonical_event_id"]) for row in events}
    if len(event_ids) != len(events):
        raise SnapshotVerificationError("canonical event identities must be unique")
    for row in events:
        if str(row.get("sport_code")) != sport_code:
            raise SnapshotVerificationError("canonical event sport mismatch with registration")

    observation_ids: set[str] = set()
    semantic_keys: set[tuple[object, ...]] = set()
    for row in quotes:
        identity = verify_quote_row_identity(row)
        if identity.provider_id != provider_id:
            raise SnapshotVerificationError("quote provider_id mismatch with registration")
        if identity.canonical_event_id not in event_ids:
            raise SnapshotVerificationError("quote references unresolved canonical event")
        if identity.canonical_event_id not in resolved_event_ids:
            raise SnapshotVerificationError(
                "quote references unresolved source event reconciliation"
            )
        source_file_sha256 = str(row.get("source_file_sha256", ""))
        if source_file_sha256 not in capture_checksums:
            raise SnapshotVerificationError(
                "quote source_file_sha256 mismatch with capture manifest"
            )
        if identity.quote_observation_id in observation_ids:
            raise SnapshotVerificationError("quote_observation_id must be unique")
        observation_ids.add(identity.quote_observation_id)
        semantic_key = quote_semantic_identity_key(identity)
        if semantic_key in semantic_keys:
            raise SnapshotVerificationError("conflicting duplicate quote identity in snapshot")
        semantic_keys.add(semantic_key)
        if str(row.get("sport_code")) != sport_code:
            raise SnapshotVerificationError("quote sport mismatch with registration")

    for row in drift_findings:
        if str(row.get("provider_id")) != provider_id:
            raise SnapshotVerificationError("drift finding provider_id mismatch")
        if str(row.get("acquisition_cycle_id")) != acquisition_cycle_id:
            raise SnapshotVerificationError("drift finding acquisition_cycle_id mismatch")

    for row in eligibility:
        if str(row.get("provider_id")) != provider_id:
            raise SnapshotVerificationError("eligibility provider_id mismatch")

    return len(events), len(quotes)


def _verify_source_participant_graph(
    *,
    source_participants: list[dict[str, Any]],
    participant_reconciliations: list[dict[str, Any]],
    sport_code: str,
) -> None:
    source_ids = {str(row["source_participant_id"]) for row in source_participants}
    if len(source_ids) != len(source_participants):
        raise SnapshotVerificationError("source participant identities must be unique")
    reconciliation_by_source = {
        str(row["source_participant_id"]): row for row in participant_reconciliations
    }
    if set(reconciliation_by_source) != source_ids:
        raise SnapshotVerificationError("participant reconciliation coverage mismatch")
    for row in source_participants:
        reconciliation = reconciliation_by_source[str(row["source_participant_id"])]
        if str(reconciliation.get("source_participant_id")) != str(row["source_participant_id"]):
            raise SnapshotVerificationError("participant reconciliation source id mismatch")


def _verify_source_event_graph(
    *,
    source_events: list[dict[str, Any]],
    event_reconciliations: list[dict[str, Any]],
    sport_code: str,
) -> set[str]:
    source_ids = {str(row["source_event_id"]) for row in source_events}
    if len(source_ids) != len(source_events):
        raise SnapshotVerificationError("source event identities must be unique")
    reconciliation_by_source = {str(row["source_event_id"]): row for row in event_reconciliations}
    if set(reconciliation_by_source) != source_ids:
        raise SnapshotVerificationError("event reconciliation coverage mismatch")
    resolved_event_ids: set[str] = set()
    for row in source_events:
        reconciliation = reconciliation_by_source[str(row["source_event_id"])]
        state = str(reconciliation.get("reconciliation_state"))
        canonical_event_id = reconciliation.get("canonical_event_id")
        if state == ReconciliationState.UNRESOLVED.value:
            if canonical_event_id is not None:
                raise SnapshotVerificationError(
                    "unresolved reconciliation must not claim canonical event"
                )
            continue
        if canonical_event_id is None:
            raise SnapshotVerificationError("resolved reconciliation requires canonical_event_id")
        if str(canonical_event_id) != str(row.get("canonical_event_id")):
            raise SnapshotVerificationError(
                "source event canonical id mismatch with reconciliation"
            )
        resolved_event_ids.add(str(canonical_event_id))
    return resolved_event_ids

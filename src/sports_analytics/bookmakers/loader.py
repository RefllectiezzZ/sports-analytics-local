"""Strict bookmaker snapshot loader with semantic verification."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from sports_analytics.bookmakers.canonical_mapping import (
    canonical_market_definition_id_from_row,
    quote_is_comparable,
)
from sports_analytics.bookmakers.native_inventory import (
    DATASET_PROVIDER_NATIVE_EVENTS,
    DATASET_PROVIDER_NATIVE_MARKETS,
    DATASET_PROVIDER_NATIVE_SELECTIONS,
)
from sports_analytics.bookmakers.schemas import (
    DATASET_ACQUISITION_METADATA,
    DATASET_CANONICAL_EVENTS,
    DATASET_COMPARISON_ELIGIBILITY,
    DATASET_PARSER_DRIFT_FINDINGS,
    DATASET_PROVIDER_STATUS,
    bookmaker_snapshot_suite,
)
from sports_analytics.bookmakers.snapshots import parse_bookmaker_source_version
from sports_analytics.bookmakers.types import (
    BOOKMAKER_SCHEMA_VERSION,
    BOOKMAKER_SCHEMA_VERSION_V2,
    BOOKMAKER_SNAPSHOT_TYPE,
    SUPPORTED_BOOKMAKER_SNAPSHOT_SCHEMAS,
)
from sports_analytics.bookmakers.verified_evidence import (
    VerifiedBookmakerQuote,
    VerifiedQuoteCatalogue,
    bookmaker_quote_identity_from_row,
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
    native_event_count: int = 0
    native_market_count: int = 0
    native_selection_count: int = 0
    verified_quotes_by_observation_id: tuple[tuple[str, VerifiedBookmakerQuote], ...] = ()
    verified_quotes_by_semantic_identity: tuple[
        tuple[tuple[object, ...], VerifiedBookmakerQuote], ...
    ] = ()
    catalogue: VerifiedQuoteCatalogue | None = None


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
    if record.schema_version not in SUPPORTED_BOOKMAKER_SNAPSHOT_SCHEMAS:
        raise SnapshotVerificationError(
            f"snapshot {snapshot_id} has unexpected schema {record.schema_version!r}"
        )

    sport_code = str(registration["sport"])
    provider_id = str(registration["provider_id"])
    acquisition_cycle_id = str(registration["acquisition_cycle_id"])
    result = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=record.relative_path,
        suite=bookmaker_snapshot_suite(
            sport_code=sport_code,
            schema_version=record.schema_version,
        ),
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
    native_counts = (0, 0, 0)
    if record.schema_version == BOOKMAKER_SCHEMA_VERSION_V2:
        native_counts = _verify_native_inventory(
            snapshot_dir=snapshot_dir,
            provider_id=provider_id,
            sport_code=sport_code,
            capture_checksums={entry.checksum_sha256 for entry in manifest.entries},
        )
    event_count, quote_count, verified_by_observation, verified_by_semantic = (
        _verify_semantic_datasets(
            snapshot_dir=snapshot_dir,
            verification=result,
            sport_code=sport_code,
            provider_id=provider_id,
            acquisition_cycle_id=acquisition_cycle_id,
            capture_checksums={entry.checksum_sha256 for entry in manifest.entries},
            snapshot_id=snapshot_id,
            checksum_sha256=result.manifest_checksum_sha256,
            schema_version=record.schema_version,
            native_event_count=native_counts[0],
        )
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
        native_event_count=native_counts[0],
        native_market_count=native_counts[1],
        native_selection_count=native_counts[2],
        verified_quotes_by_observation_id=verified_by_observation,
        verified_quotes_by_semantic_identity=verified_by_semantic,
        catalogue=VerifiedQuoteCatalogue(
            snapshot_id=snapshot_id,
            snapshot_checksum_sha256=result.manifest_checksum_sha256,
            provider_id=provider_id,
            sport=sport_code,
            quotes_by_observation_id=verified_by_observation,
            quotes_by_semantic_identity=verified_by_semantic,
        ),
    )


def load_verified_bookmaker_quotes(
    *,
    database_connection: sqlite3.Connection,
    snapshots_directory: Path,
    raw_directory: Path,
    snapshot_id: str,
) -> LoadedBookmakerSnapshot:
    """Load and verify a snapshot, returning immutable verified quote evidence."""
    loaded = load_bookmaker_snapshot(
        database_connection=database_connection,
        snapshots_directory=snapshots_directory,
        raw_directory=raw_directory,
        snapshot_id=snapshot_id,
    )
    if not loaded.verified:
        raise SnapshotVerificationError("snapshot verification did not produce verified quotes")
    return loaded


def _read_dataset(snapshot_dir: Path, dataset_name: str) -> list[dict[str, Any]]:
    path = snapshot_dir / f"{dataset_name}.parquet"
    if path.is_symlink():
        raise SnapshotVerificationError(f"dataset must not be a symlink: {dataset_name}")
    if not path.is_file():
        raise SnapshotVerificationError(f"required dataset missing: {dataset_name}")
    table = pq.read_table(path)
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows


def _verify_native_inventory(
    *,
    snapshot_dir: Path,
    provider_id: str,
    sport_code: str,
    capture_checksums: set[str],
) -> tuple[int, int, int]:
    """Verify native event -> market -> selection graph and evidence linkage."""
    events = _read_dataset(snapshot_dir, DATASET_PROVIDER_NATIVE_EVENTS)
    markets = _read_dataset(snapshot_dir, DATASET_PROVIDER_NATIVE_MARKETS)
    selections = _read_dataset(snapshot_dir, DATASET_PROVIDER_NATIVE_SELECTIONS)
    if not events or not markets or not selections:
        raise SnapshotVerificationError(
            "provider-native snapshot requires non-empty events, markets, and selections"
        )
    event_ids = [str(row.get("source_event_id", "")) for row in events]
    if any(not value for value in event_ids) or len(event_ids) != len(set(event_ids)):
        raise SnapshotVerificationError("provider-native event identities must be unique")
    event_set = set(event_ids)
    market_keys = [
        (str(row.get("source_event_id", "")), str(row.get("source_market_id", "")))
        for row in markets
    ]
    if any(not event_id or not market_id for event_id, market_id in market_keys):
        raise SnapshotVerificationError("provider-native market identity is missing")
    if len(market_keys) != len(set(market_keys)):
        raise SnapshotVerificationError("provider-native market identities must be unique")
    if any(event_id not in event_set for event_id, _ in market_keys):
        raise SnapshotVerificationError("provider-native market orphan")
    market_set = set(market_keys)
    selection_keys = [
        (
            str(row.get("source_event_id", "")),
            str(row.get("source_market_id", "")),
            str(row.get("source_selection_id", "")),
        )
        for row in selections
    ]
    if any(
        not event_id or not market_id or not selection_id
        for event_id, market_id, selection_id in selection_keys
    ):
        raise SnapshotVerificationError("provider-native selection identity is missing")
    if len(selection_keys) != len(set(selection_keys)):
        raise SnapshotVerificationError("provider-native selection identities must be unique")
    if any((event_id, market_id) not in market_set for event_id, market_id, _ in selection_keys):
        raise SnapshotVerificationError("provider-native selection orphan")

    for row in (*events, *markets, *selections):
        if str(row.get("provider_id")) != provider_id:
            raise SnapshotVerificationError("provider-native provider mismatch")
        if str(row.get("sport")) != sport_code:
            raise SnapshotVerificationError("provider-native sport mismatch")
    for row in events:
        try:
            references = json.loads(str(row.get("source_capture_ids", "")))
        except json.JSONDecodeError as exc:
            raise SnapshotVerificationError("native event capture references malformed") from exc
        if (
            not isinstance(references, list)
            or not references
            or any(
                not isinstance(item, str) or item not in capture_checksums for item in references
            )
        ):
            raise SnapshotVerificationError("native event capture reference mismatch")
        event_id = str(row["source_event_id"])
        event_markets = sum(1 for key in market_keys if key[0] == event_id)
        event_selections = sum(1 for key in selection_keys if key[0] == event_id)
        if int(row.get("markets_parsed", -1)) != event_markets:
            raise SnapshotVerificationError("native event parsed market count mismatch")
        if int(row.get("selections_parsed", -1)) != event_selections:
            raise SnapshotVerificationError("native event parsed selection count mismatch")
        state = str(row.get("completeness_state"))
        if state.startswith("complete-") and not (
            bool(row.get("event_detail_surface_visited"))
            and bool(row.get("event_detail_readiness_reached"))
            and int(row.get("truncated_response_count", -1)) == 0
            and int(row.get("bounded_response_rejection_count", -1)) == 0
            and int(row.get("markets_rejected", -1)) == 0
            and int(row.get("selections_rejected", -1)) == 0
        ):
            raise SnapshotVerificationError("native complete state lacks clean detail evidence")
    for row in (*markets, *selections):
        capture_id = row.get("source_capture_id")
        if not isinstance(capture_id, str) or capture_id not in capture_checksums:
            raise SnapshotVerificationError("native row capture reference mismatch")
    for row in selections:
        try:
            odds = Decimal(str(row.get("decimal_odds")))
        except InvalidOperation as exc:
            raise SnapshotVerificationError("native decimal odds malformed") from exc
        if not odds.is_finite() or odds <= Decimal("1"):
            raise SnapshotVerificationError("native decimal odds invalid")
        _verify_optional_decimal(row.get("selection_line"), field_name="selection_line")
    for row in markets:
        _verify_optional_decimal(row.get("market_line"), field_name="market_line")
    expected_market_order = sorted(
        markets,
        key=lambda row: (
            str(row["source_event_id"]),
            int(row["provider_order"]),
            str(row["source_market_id"]),
        ),
    )
    if markets != expected_market_order:
        raise SnapshotVerificationError("provider-native markets are not deterministically ordered")
    expected_selection_order = sorted(
        selections,
        key=lambda row: (
            str(row["source_event_id"]),
            str(row["source_market_id"]),
            int(row["provider_order"]),
            str(row["source_selection_id"]),
        ),
    )
    if selections != expected_selection_order:
        raise SnapshotVerificationError(
            "provider-native selections are not deterministically ordered"
        )
    return len(events), len(markets), len(selections)


def _verify_optional_decimal(value: object, *, field_name: str) -> None:
    if value is None:
        return
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise SnapshotVerificationError(f"native {field_name} malformed") from exc
    if not parsed.is_finite():
        raise SnapshotVerificationError(f"native {field_name} must be finite")


def _verify_semantic_datasets(
    *,
    snapshot_dir: Path,
    verification: SnapshotVerificationResult,
    sport_code: str,
    provider_id: str,
    acquisition_cycle_id: str,
    capture_checksums: set[str],
    snapshot_id: str,
    checksum_sha256: str,
    schema_version: str,
    native_event_count: int,
) -> tuple[
    int,
    int,
    tuple[tuple[str, VerifiedBookmakerQuote], ...],
    tuple[tuple[tuple[object, ...], VerifiedBookmakerQuote], ...],
]:
    suite = bookmaker_snapshot_suite(
        sport_code=sport_code,
        schema_version=schema_version,
    )
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
    expected_event_count = (
        native_event_count if schema_version == BOOKMAKER_SCHEMA_VERSION_V2 else len(events)
    )
    if int(acquisition.get("event_count", -1)) != expected_event_count:
        raise SnapshotVerificationError("acquisition metadata event_count mismatch")

    provider_status_for_provider = [
        row for row in provider_status_rows if str(row.get("provider_id")) == provider_id
    ]
    if len(provider_status_rows) != 1:
        raise SnapshotVerificationError("provider status must contain exactly one admitted row")
    if len(provider_status_for_provider) != 1:
        raise SnapshotVerificationError("provider status must contain exactly one provider row")
    provider_status = provider_status_for_provider[0]
    if str(provider_status.get("provider_id")) != provider_id:
        raise SnapshotVerificationError("provider status provider_id mismatch")
    if int(provider_status.get("valid_quotes_observed", -1)) != len(quotes):
        raise SnapshotVerificationError("provider status valid_quotes_observed mismatch")
    if int(provider_status.get("events_observed", -1)) != expected_event_count:
        raise SnapshotVerificationError("provider status events_observed mismatch")

    _verify_source_participant_graph(
        source_participants=source_participants,
        participant_reconciliations=participant_reconciliations,
        sport_code=sport_code,
        provider_id=provider_id,
    )
    resolved_event_ids = _verify_source_event_graph(
        source_events=source_events,
        event_reconciliations=event_reconciliations,
        sport_code=sport_code,
        provider_id=provider_id,
        capture_checksums=capture_checksums,
        source_participants=source_participants,
        participant_reconciliations=participant_reconciliations,
        canonical_events=events,
    )

    if schema_version == BOOKMAKER_SCHEMA_VERSION and (len(events) < 1 or len(quotes) < 1):
        raise SnapshotVerificationError("admitted snapshot must contain events and quotes")
    event_ids = {str(row["canonical_event_id"]) for row in events}
    if len(event_ids) != len(events):
        raise SnapshotVerificationError("canonical event identities must be unique")
    for row in events:
        if str(row.get("sport_code")) != sport_code:
            raise SnapshotVerificationError("canonical event sport mismatch with registration")
        home_canonical = row.get("home_canonical_participant_id")
        away_canonical = row.get("away_canonical_participant_id")
        if home_canonical is None or away_canonical is None:
            raise SnapshotVerificationError("canonical event requires participant correspondence")

    observation_ids: set[str] = set()
    semantic_keys: set[tuple[object, ...]] = set()
    verified_by_observation: dict[str, VerifiedBookmakerQuote] = {}
    verified_by_semantic: dict[tuple[object, ...], VerifiedBookmakerQuote] = {}
    eligibility_by_observation = {
        str(row["quote_observation_id"]): row
        for row in eligibility
        if row.get("quote_observation_id")
    }
    if len(eligibility_by_observation) != len(eligibility):
        raise SnapshotVerificationError("eligibility quote_observation_id must be unique")
    if set(eligibility_by_observation) != {str(row["quote_observation_id"]) for row in quotes}:
        raise SnapshotVerificationError(
            "eligibility coverage must match quote observations exactly"
        )

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
        if str(row.get("sport_code")) != sport_code:
            raise SnapshotVerificationError("quote sport mismatch with registration")
        source_event_id = str(row.get("source_event_id", ""))
        if not source_event_id:
            raise SnapshotVerificationError("quote requires source_event_id")
        source_event_rows = [
            item for item in source_events if str(item.get("source_event_id")) == source_event_id
        ]
        if len(source_event_rows) != 1:
            raise SnapshotVerificationError("quote source_event_id missing from source events")
        source_event = source_event_rows[0]
        if str(source_event.get("source_name")) != provider_id:
            raise SnapshotVerificationError("quote source event provider mismatch")
        if str(source_event.get("sport_code")) != sport_code:
            raise SnapshotVerificationError("quote source event sport mismatch")
        if str(source_event.get("canonical_event_id")) != identity.canonical_event_id:
            raise SnapshotVerificationError(
                "quote canonical event does not match reconciled source event"
            )
        eligibility_row = eligibility_by_observation[identity.quote_observation_id]
        overtime_scope = (
            None
            if eligibility_row.get("overtime_scope") is None
            else str(eligibility_row["overtime_scope"])
        )
        rules_scope = (
            None
            if eligibility_row.get("rules_scope") is None
            else str(eligibility_row["rules_scope"])
        )
        verified = _build_verified_quote_from_loaded_row(
            loaded_snapshot_id=snapshot_id,
            loaded_checksum_sha256=checksum_sha256,
            loaded_provider_id=provider_id,
            loaded_sport=sport_code,
            quote_row=row,
            overtime_scope=overtime_scope,
            rules_scope=rules_scope,
            comparable=bool(eligibility_row.get("comparable")),
        )
        semantic_key = quote_semantic_identity_key(verified.identity)
        if semantic_key in semantic_keys:
            raise SnapshotVerificationError("conflicting duplicate quote identity in snapshot")
        semantic_keys.add(semantic_key)
        _validate_eligibility_matches_quote(eligibility_row, verified)
        verified_by_observation[identity.quote_observation_id] = verified
        verified_by_semantic[semantic_key] = verified

    for row in drift_findings:
        if str(row.get("provider_id")) != provider_id:
            raise SnapshotVerificationError("drift finding provider_id mismatch")
        if str(row.get("acquisition_cycle_id")) != acquisition_cycle_id:
            raise SnapshotVerificationError("drift finding acquisition_cycle_id mismatch")

    return (
        len(events),
        len(quotes),
        tuple(sorted(verified_by_observation.items())),
        tuple(sorted(verified_by_semantic.items())),
    )


def _verify_source_participant_graph(
    *,
    source_participants: list[dict[str, Any]],
    participant_reconciliations: list[dict[str, Any]],
    sport_code: str,
    provider_id: str,
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
        if str(row.get("source_name")) != provider_id:
            raise SnapshotVerificationError("source participant provider identity mismatch")
        reconciliation = reconciliation_by_source[str(row["source_participant_id"])]
        if str(reconciliation.get("source_participant_id")) != str(row["source_participant_id"]):
            raise SnapshotVerificationError("participant reconciliation source id mismatch")
        if str(reconciliation.get("source_name")) != provider_id:
            raise SnapshotVerificationError("participant reconciliation provider mismatch")
        if not str(reconciliation.get("reconciliation_policy_version") or "").strip():
            raise SnapshotVerificationError(
                "participant reconciliation requires reconciliation_policy_version"
            )
        if reconciliation.get("source_observed_at_utc") is None:
            raise SnapshotVerificationError(
                "participant reconciliation requires source_observed_at_utc"
            )
        state = str(reconciliation.get("reconciliation_state"))
        if state == ReconciliationState.UNRESOLVED.value:
            if reconciliation.get("canonical_participant_id") is not None:
                raise SnapshotVerificationError(
                    "unresolved participant reconciliation must not claim canonical id"
                )
        elif reconciliation.get("canonical_participant_id") is None:
            raise SnapshotVerificationError(
                "resolved participant reconciliation requires canonical_participant_id"
            )


def _verify_source_event_graph(
    *,
    source_events: list[dict[str, Any]],
    event_reconciliations: list[dict[str, Any]],
    sport_code: str,
    provider_id: str,
    capture_checksums: set[str],
    source_participants: list[dict[str, Any]],
    participant_reconciliations: list[dict[str, Any]],
    canonical_events: list[dict[str, Any]],
) -> set[str]:
    source_ids = {str(row["source_event_id"]) for row in source_events}
    if len(source_ids) != len(source_events):
        raise SnapshotVerificationError("source event identities must be unique")
    reconciliation_by_source = {str(row["source_event_id"]): row for row in event_reconciliations}
    if set(reconciliation_by_source) != source_ids:
        raise SnapshotVerificationError("event reconciliation coverage mismatch")
    participant_ids = {str(row["source_participant_id"]) for row in source_participants}
    participant_recon_by_source = {
        str(row["source_participant_id"]): row for row in participant_reconciliations
    }
    canonical_by_id = {str(row["canonical_event_id"]): row for row in canonical_events}
    resolved_event_ids: set[str] = set()
    for row in source_events:
        if str(row.get("source_name")) != provider_id:
            raise SnapshotVerificationError("source event provider identity mismatch")
        if str(row.get("sport_code")) != sport_code:
            raise SnapshotVerificationError("source event sport mismatch")
        if row.get("source_observed_at_utc") is None:
            raise SnapshotVerificationError("source event requires source_observed_at_utc")
        source_checksum = row.get("source_file_sha256")
        if not isinstance(source_checksum, str) or source_checksum not in capture_checksums:
            raise SnapshotVerificationError(
                "source event source_file_sha256 mismatch with capture manifest"
            )
        home_source = row.get("home_source_participant_id")
        away_source = row.get("away_source_participant_id")
        if home_source is None or away_source is None:
            raise SnapshotVerificationError("source event requires participant references")
        home_source_id = str(home_source)
        away_source_id = str(away_source)
        if home_source_id not in participant_ids or away_source_id not in participant_ids:
            raise SnapshotVerificationError(
                "source event participant references missing from source participants"
            )
        reconciliation = reconciliation_by_source[str(row["source_event_id"])]
        if str(reconciliation.get("source_name")) != provider_id:
            raise SnapshotVerificationError("event reconciliation provider mismatch")
        if not str(reconciliation.get("reconciliation_policy_version") or "").strip():
            raise SnapshotVerificationError(
                "event reconciliation requires reconciliation_policy_version"
            )
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
        home_canonical = row.get("home_canonical_participant_id")
        away_canonical = row.get("away_canonical_participant_id")
        if home_canonical is None or away_canonical is None:
            raise SnapshotVerificationError(
                "resolved source event requires canonical participant correspondence"
            )
        home_recon = participant_recon_by_source[home_source_id]
        away_recon = participant_recon_by_source[away_source_id]
        if str(home_recon.get("canonical_participant_id")) != str(home_canonical):
            raise SnapshotVerificationError(
                "home source participant reconciliation does not match event home canonical"
            )
        if str(away_recon.get("canonical_participant_id")) != str(away_canonical):
            raise SnapshotVerificationError(
                "away source participant reconciliation does not match event away canonical"
            )
        canonical_event = canonical_by_id.get(str(canonical_event_id))
        if canonical_event is None:
            raise SnapshotVerificationError("canonical event missing for reconciled source event")
        if str(canonical_event.get("home_canonical_participant_id")) != str(home_canonical):
            raise SnapshotVerificationError(
                "canonical event home participant mismatch with source event"
            )
        if str(canonical_event.get("away_canonical_participant_id")) != str(away_canonical):
            raise SnapshotVerificationError(
                "canonical event away participant mismatch with source event"
            )
        resolved_event_ids.add(str(canonical_event_id))
    return resolved_event_ids


def _validate_eligibility_matches_quote(
    eligibility_row: dict[str, Any],
    verified: VerifiedBookmakerQuote,
) -> None:
    identity = verified.identity
    line = None if identity.line_value is None else format(identity.line_value, "f")
    if str(eligibility_row.get("provider_id")) != verified.provider_id:
        raise SnapshotVerificationError("eligibility provider_id mismatch")
    if str(eligibility_row["canonical_event_id"]) != identity.canonical_event_id:
        raise SnapshotVerificationError("eligibility event mismatch with quote")
    if str(eligibility_row["canonical_market_definition_id"]) != (
        verified.canonical_market_definition_id
    ):
        raise SnapshotVerificationError("eligibility market definition mismatch with quote")
    if str(eligibility_row["canonical_selection_id"]) != verified.canonical_selection_id:
        raise SnapshotVerificationError("eligibility selection mismatch with quote")
    if str(eligibility_row.get("line_type")) != identity.line_type:
        raise SnapshotVerificationError("eligibility line_type mismatch with quote")
    eligibility_line = eligibility_row.get("line_value")
    eligibility_line_text = None if eligibility_line is None else str(eligibility_line)
    if eligibility_line_text != line:
        raise SnapshotVerificationError("eligibility line_value mismatch with quote")
    if str(eligibility_row.get("market_period")) != identity.market_period:
        raise SnapshotVerificationError("eligibility market_period mismatch with quote")
    if str(eligibility_row.get("participant_scope")) != identity.participant_scope:
        raise SnapshotVerificationError("eligibility participant_scope mismatch with quote")
    eligibility_participant = eligibility_row.get("canonical_participant_id")
    if (None if eligibility_participant is None else str(eligibility_participant)) != (
        identity.canonical_participant_id
    ):
        raise SnapshotVerificationError("eligibility canonical_participant_id mismatch")
    eligibility_overtime = eligibility_row.get("overtime_scope")
    if (None if eligibility_overtime is None else str(eligibility_overtime)) != (
        identity.overtime_scope
    ):
        raise SnapshotVerificationError("eligibility overtime_scope mismatch with quote")
    eligibility_rules = eligibility_row.get("rules_scope")
    if (None if eligibility_rules is None else str(eligibility_rules)) != identity.rules_scope:
        raise SnapshotVerificationError("eligibility rules_scope mismatch with quote")
    if bool(eligibility_row.get("comparable")) != verified.comparable:
        raise SnapshotVerificationError("eligibility comparable mismatch with quote")
    if verified.comparable and not quote_is_comparable(
        definition_id=verified.canonical_market_definition_id,
        overtime_scope=identity.overtime_scope,
        rules_scope=identity.rules_scope,
    ):
        raise SnapshotVerificationError("comparable quote missing required rules evidence")


def _build_verified_quote_from_loaded_row(
    *,
    loaded_snapshot_id: str,
    loaded_checksum_sha256: str,
    loaded_provider_id: str,
    loaded_sport: str,
    quote_row: dict[str, Any],
    overtime_scope: str | None,
    rules_scope: str | None,
    comparable: bool,
) -> VerifiedBookmakerQuote:
    """Private loader-only constructor for verified quote evidence."""
    from datetime import datetime
    from decimal import Decimal

    from sports_analytics.markets.contracts import validate_decimal_odds
    from sports_analytics.sports.contracts import require_utc

    identity = bookmaker_quote_identity_from_row(
        quote_row,
        overtime_scope=overtime_scope,
        rules_scope=rules_scope,
    )
    if identity.provider_id != loaded_provider_id:
        msg = "quote provider_id does not match verified snapshot registration"
        raise SnapshotVerificationError(msg)
    sport = str(quote_row.get("sport_code", loaded_sport))
    if sport != loaded_sport:
        msg = "quote sport does not match verified snapshot registration"
        raise SnapshotVerificationError(msg)
    observed_raw = quote_row.get("source_observed_at_utc")
    if not isinstance(observed_raw, datetime):
        msg = "quote row requires source_observed_at_utc timestamp"
        raise SnapshotVerificationError(msg)
    canonical_market_definition_id = canonical_market_definition_id_from_row(quote_row)
    expected_comparable = quote_is_comparable(
        definition_id=canonical_market_definition_id,
        overtime_scope=overtime_scope,
        rules_scope=rules_scope,
    )
    if comparable != expected_comparable:
        raise SnapshotVerificationError("eligibility comparable flag mismatch with scopes")
    market_status = quote_row.get("market_status")
    selection_status = quote_row.get("selection_status")
    if not isinstance(market_status, str) or not market_status.strip():
        raise SnapshotVerificationError("quote row requires market_status")
    if not isinstance(selection_status, str) or not selection_status.strip():
        raise SnapshotVerificationError("quote row requires selection_status")
    source_event_id = quote_row.get("source_event_id")
    if not isinstance(source_event_id, str) or not source_event_id.strip():
        raise SnapshotVerificationError("quote requires source_event_id")
    source_file_sha256 = quote_row.get("source_file_sha256")
    if not isinstance(source_file_sha256, str) or not source_file_sha256.strip():
        raise SnapshotVerificationError("quote requires source_file_sha256")
    return VerifiedBookmakerQuote(
        snapshot_id=loaded_snapshot_id,
        snapshot_checksum_sha256=loaded_checksum_sha256,
        provider_id=loaded_provider_id,
        sport=loaded_sport,
        identity=identity,
        decimal_odds=validate_decimal_odds(Decimal(str(quote_row["decimal_odds"]))),
        observed_at_utc=require_utc(observed_raw, field_name="source_observed_at_utc"),
        market_status=market_status,
        selection_status=selection_status,
        source_file_sha256=source_file_sha256,
        canonical_market_definition_id=canonical_market_definition_id,
        canonical_selection_id=str(quote_row.get("outcome_key", "")),
        source_event_id=source_event_id,
        comparable=comparable,
    )

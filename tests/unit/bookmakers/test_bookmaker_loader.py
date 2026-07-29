"""Strict bookmaker snapshot loader tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.bookmakers.loader import load_bookmaker_snapshot
from sports_analytics.bookmakers.normalization import normalize_bookmaker_bundles
from sports_analytics.bookmakers.reconciliation import reconcile_bookmaker_bundles
from sports_analytics.bookmakers.snapshots import (
    build_bookmaker_source_version,
    publish_bookmaker_snapshot,
)
from sports_analytics.bookmakers.status import build_provider_status
from sports_analytics.bookmakers.types import FailureClassification
from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER
from sports_analytics.sources.betano.synthetic import parse_betano_synthetic_payloads
from sports_analytics.sources.bookmaker_capture import (
    attach_capture_references,
    build_capture_manifest,
    manifest_to_raw_artifact,
    persist_capture_manifest,
)
from sports_analytics.sources.bookmaker_contracts import (
    ProviderMarketObservation,
    ProviderSelectionObservation,
    ProviderSelectionPriceState,
)
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "betano"


def _provider_status_for_bundle(bundle, *, snapshot_id: str | None):
    event_count = len(bundle.events)
    quote_count = len(bundle.market_quotes)
    return (
        build_provider_status(
            provider_id="betano-pt",
            adapter_version=BETANO_ADAPTER,
            observed_at_utc=OBSERVED,
            last_attempted_acquisition_utc=OBSERVED,
            last_successful_acquisition_utc=OBSERVED,
            last_valid_snapshot_id=snapshot_id,
            snapshot_age_seconds=0,
            events_observed=event_count,
            valid_quotes_observed=quote_count,
            unresolved_events=0,
            rejected_markets=0,
            warnings=(),
            current_block_or_failure_classification=FailureClassification.NONE,
            next_eligible_attempt_utc=None,
        ),
    )


def _published_snapshot(tmp_path: Path):
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    snapshots = tmp_path / "snapshots"
    raw = tmp_path / "raw"
    payload = json.loads((FIXTURES / "football.json").read_text(encoding="utf-8"))
    bundle = parse_betano_synthetic_payloads(
        [payload],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-loader",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    reconciled = reconcile_bookmaker_bundles((bundle,))
    store = BookmakerRawCaptureStore(raw)
    capture = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content=(FIXTURES / "football.json").read_text(encoding="utf-8"),
        retrieved_at=OBSERVED,
        extension="json",
    )
    normalized = normalize_bookmaker_bundles(
        (bundle,),
        reconciliations=reconciled,
        source_file_sha256=capture.checksum_sha256,
    )
    manifest = persist_capture_manifest(
        raw_directory=raw,
        manifest=build_capture_manifest(
            provider_id="betano-pt",
            acquisition_cycle_id="cycle-loader",
            captures=(capture,),
        ),
    )
    source_version = build_bookmaker_source_version(
        sport_code="football",
        acquisition_cycle_id="cycle-loader",
        raw_sha256=manifest.checksum_sha256,
    )
    publication = publish_bookmaker_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        sport_code="football",
        source_version=source_version,
        source_observed_at_utc=OBSERVED,
        bundle=normalized,
        provider_statuses=_provider_status_for_bundle(normalized, snapshot_id=None),
        raw_artifact=manifest_to_raw_artifact(manifest),
        domain_metadata={
            "capture_manifest_relative_path": manifest.relative_path,
            "capture_manifest_checksum_sha256": manifest.checksum_sha256,
        },
    )
    with connect_database(database) as connection:
        from sports_analytics.data.database import transaction
        from sports_analytics.data.repositories.bookmakers import BookmakerRepository

        with transaction(connection, immediate=True):
            repo = BookmakerRepository(connection)
            repo.register_snapshot(
                snapshot_id=publication.published.snapshot_id,
                provider_id="betano-pt",
                sport="football",
                schema_version=publication.published.schema_version,
                checksum_sha256=publication.published.manifest_checksum_sha256,
                relative_path=publication.published.snapshot_relative_path,
                observed_at=OBSERVED,
                registered_at=OBSERVED,
                acquisition_cycle_id="cycle-loader",
            )
    return database, raw, snapshots, publication.published.snapshot_id


def _published_native_snapshot(
    tmp_path: Path,
    *,
    native_market_count_override: int | None = None,
):
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    snapshots = tmp_path / "snapshots"
    raw = tmp_path / "raw"
    payload = json.loads((FIXTURES / "football.json").read_text(encoding="utf-8"))
    bundle = parse_betano_synthetic_payloads(
        [payload],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-native-loader",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    store = BookmakerRawCaptureStore(raw)
    captures = tuple(
        store.store_text(
            source_name="betano-pt",
            capture_kind="provider-json",
            content=json.dumps({"capture": index}),
            retrieved_at=OBSERVED,
            extension="json",
        )
        for index in (1, 2)
    )
    original_event = bundle.events[0]
    native_markets = tuple(
        replace(
            market,
            source_capture_id=(
                captures[0].checksum_sha256 if index < 2 else captures[1].checksum_sha256
            ),
            selections=tuple(
                replace(
                    selection,
                    source_capture_id=(
                        captures[0].checksum_sha256 if index < 2 else captures[1].checksum_sha256
                    ),
                )
                for selection in market.selections
            ),
        )
        for index, market in enumerate(original_event.markets)
    )
    native_markets += tuple(
        ProviderMarketObservation(
            source_market_id=f"native-unknown-{index}",
            display_label=f"Native unknown {index}",
            market_status=MarketStatus.OPEN,
            selections=(
                ProviderSelectionObservation(
                    source_selection_id=f"native-selection-{index}",
                    display_label=f"Native selection {index}",
                    decimal_odds=None if index == 6 else Decimal("2.00"),
                    selection_status=SelectionStatus.SUSPENDED
                    if index == 6
                    else SelectionStatus.ACTIVE,
                    price_state=(
                        ProviderSelectionPriceState.UNPRICED
                        if index == 6
                        else ProviderSelectionPriceState.PRICED
                    ),
                    source_capture_id=captures[1].checksum_sha256,
                ),
            ),
            provider_market_type=f"UNKNOWN-{index}",
            source_capture_id=captures[1].checksum_sha256,
        )
        for index in range(7)
    )
    bundle = replace(
        bundle,
        events=(
            replace(
                original_event,
                markets=native_markets[:3],
                native_markets=native_markets,
            ),
        ),
    )
    bundle = attach_capture_references(bundle, captures)
    normalized = normalize_bookmaker_bundles(
        (bundle,),
        reconciliations=reconcile_bookmaker_bundles((bundle,)),
        source_file_sha256=captures[0].checksum_sha256,
    )
    manifest = persist_capture_manifest(
        raw_directory=raw,
        manifest=build_capture_manifest(
            provider_id="betano-pt",
            acquisition_cycle_id="cycle-native-loader",
            captures=captures,
        ),
    )
    source_version = build_bookmaker_source_version(
        sport_code="football",
        acquisition_cycle_id="cycle-native-loader",
        raw_sha256=manifest.checksum_sha256,
    )
    domain_metadata: dict[str, object] = {
        "capture_manifest_relative_path": manifest.relative_path,
        "capture_manifest_checksum_sha256": manifest.checksum_sha256,
    }
    if native_market_count_override is not None:
        domain_metadata["provider_native_market_count"] = native_market_count_override
    publication = publish_bookmaker_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        sport_code="football",
        source_version=source_version,
        source_observed_at_utc=OBSERVED,
        bundle=normalized,
        provider_statuses=_provider_status_for_bundle(normalized, snapshot_id=None),
        raw_artifact=manifest_to_raw_artifact(manifest),
        domain_metadata=domain_metadata,
        provider_bundle=bundle,
    )
    with connect_database(database) as connection:
        from sports_analytics.data.database import transaction
        from sports_analytics.data.repositories.bookmakers import BookmakerRepository

        with transaction(connection, immediate=True):
            BookmakerRepository(connection).register_snapshot(
                snapshot_id=publication.published.snapshot_id,
                provider_id="betano-pt",
                sport="football",
                schema_version=publication.published.schema_version,
                checksum_sha256=publication.published.manifest_checksum_sha256,
                relative_path=publication.published.snapshot_relative_path,
                observed_at=OBSERVED,
                registered_at=OBSERVED,
                acquisition_cycle_id="cycle-native-loader",
            )
    return (
        database,
        raw,
        snapshots,
        publication.published.snapshot_id,
        captures,
    )


def test_load_bookmaker_snapshot_verifies_manifest_and_datasets(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    with connect_database(database) as connection:
        loaded = load_bookmaker_snapshot(
            database_connection=connection,
            snapshots_directory=snapshots,
            raw_directory=raw,
            snapshot_id=snapshot_id,
        )
    assert loaded.verified is True
    assert loaded.provider_id == "betano-pt"
    assert loaded.event_count >= 1
    assert loaded.quote_count >= 1


def test_native_v2_round_trip_separates_native_and_canonical_counts(
    tmp_path: Path,
) -> None:
    database, raw, snapshots, snapshot_id, _captures = _published_native_snapshot(tmp_path)
    with connect_database(database) as connection:
        loaded = load_bookmaker_snapshot(
            database_connection=connection,
            snapshots_directory=snapshots,
            raw_directory=raw,
            snapshot_id=snapshot_id,
        )
    assert loaded.native_event_count == 1
    assert loaded.native_market_count == 10
    assert loaded.native_selection_count == 14
    assert loaded.quote_count == 7


def test_native_v2_manifest_count_mismatch_fails_closed(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id, _captures = _published_native_snapshot(
        tmp_path,
        native_market_count_override=3,
    )
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="native_market_count"):
            load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


@pytest.mark.parametrize("capture_index", [0, 1])
def test_native_v2_tampering_either_contributing_capture_fails_strict_reload(
    tmp_path: Path,
    capture_index: int,
) -> None:
    database, raw, snapshots, snapshot_id, captures = _published_native_snapshot(tmp_path)
    path = raw / captures[capture_index].relative_path
    path.write_bytes(path.read_bytes() + b"tamper")
    with connect_database(database) as connection:
        with pytest.raises((SnapshotVerificationError, SnapshotIntegrityError)):
            load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_missing_capture_manifest_metadata(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    manifest_path = next(snapshots.rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    relative = manifest["domain_metadata"]["capture_manifest_relative_path"]
    capture_manifest = raw / relative
    capture_manifest.unlink()
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="capture manifest file missing"):
            load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_tampered_capture_bytes(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    for path in raw.rglob("*.json"):
        if "manifests" not in str(path):
            path.write_text('{"tampered": true}', encoding="utf-8")
            break
    with connect_database(database) as connection:
        with pytest.raises((SnapshotVerificationError, SnapshotIntegrityError)):
            load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def _multi_event_payload() -> dict:
    payload = json.loads((FIXTURES / "football.json").read_text(encoding="utf-8"))
    second = json.loads(json.dumps(payload["events"][0]))
    second["source_event_id"] = "betano-evt-football-2"
    second["participants"][0]["source_participant_id"] = "betano-club-east"
    second["participants"][0]["display_name"] = "East United"
    second["participants"][0]["normalized_name"] = "east united"
    second["participants"][1]["source_participant_id"] = "betano-club-west"
    second["participants"][1]["display_name"] = "West Rovers"
    second["participants"][1]["normalized_name"] = "west rovers"
    payload["events"].append(second)
    return payload


def test_loader_accepts_same_market_on_distinct_events(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    snapshots = tmp_path / "snapshots"
    raw = tmp_path / "raw"
    payload = _multi_event_payload()
    bundle = parse_betano_synthetic_payloads(
        [payload],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-multi",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    reconciled = reconcile_bookmaker_bundles((bundle,))
    store = BookmakerRawCaptureStore(raw)
    capture = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content=json.dumps(payload),
        retrieved_at=OBSERVED,
        extension="json",
    )
    normalized = normalize_bookmaker_bundles(
        (bundle,),
        reconciliations=reconciled,
        source_file_sha256=capture.checksum_sha256,
    )
    manifest = persist_capture_manifest(
        raw_directory=raw,
        manifest=build_capture_manifest(
            provider_id="betano-pt",
            acquisition_cycle_id="cycle-multi",
            captures=(capture,),
        ),
    )
    source_version = build_bookmaker_source_version(
        sport_code="football",
        acquisition_cycle_id="cycle-multi",
        raw_sha256=manifest.checksum_sha256,
    )
    publication = publish_bookmaker_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        sport_code="football",
        source_version=source_version,
        source_observed_at_utc=OBSERVED,
        bundle=normalized,
        provider_statuses=_provider_status_for_bundle(normalized, snapshot_id=None),
        raw_artifact=manifest_to_raw_artifact(manifest),
        domain_metadata={
            "capture_manifest_relative_path": manifest.relative_path,
            "capture_manifest_checksum_sha256": manifest.checksum_sha256,
        },
    )
    with connect_database(database) as connection:
        from sports_analytics.data.database import transaction
        from sports_analytics.data.repositories.bookmakers import BookmakerRepository

        with transaction(connection, immediate=True):
            BookmakerRepository(connection).register_snapshot(
                snapshot_id=publication.published.snapshot_id,
                provider_id="betano-pt",
                sport="football",
                schema_version=publication.published.schema_version,
                checksum_sha256=publication.published.manifest_checksum_sha256,
                relative_path=publication.published.snapshot_relative_path,
                observed_at=OBSERVED,
                registered_at=OBSERVED,
                acquisition_cycle_id="cycle-multi",
            )
        loaded = load_bookmaker_snapshot(
            database_connection=connection,
            snapshots_directory=snapshots,
            raw_directory=raw,
            snapshot_id=publication.published.snapshot_id,
        )
    assert loaded.event_count == 2
    assert loaded.quote_count >= 6


def test_loader_rejects_duplicate_quote_observation_id(tmp_path: Path) -> None:
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    from sports_analytics.snapshots.parquet import file_sha256_and_size
    from sports_analytics.snapshots.spec import MANIFEST_FILENAME

    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    quotes_path = next(snapshots.rglob("market_quotes.parquet"))
    manifest_path = quotes_path.parent / MANIFEST_FILENAME
    table = pq.read_table(quotes_path)
    rows = table.to_pylist()
    rows.append(dict(rows[0]))
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), quotes_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status_path = quotes_path.parent / "provider_status.parquet"
    if status_path.is_file():
        status_table = pq.read_table(status_path)
        status_rows = status_table.to_pylist()
        if status_rows:
            status_rows[0]["valid_quotes_observed"] = len(rows)
            pq.write_table(
                pa.Table.from_pylist(status_rows, schema=status_table.schema),
                status_path,
            )
            status_digest, status_bytes = file_sha256_and_size(status_path)
            for entry in manifest["files"]:
                if entry["relative_filename"] == "provider_status.parquet":
                    entry["sha256"] = status_digest
                    entry["byte_count"] = status_bytes
            if "row_counts" in manifest:
                manifest["row_counts"]["provider_status"] = len(status_rows)
    digest, byte_count = file_sha256_and_size(quotes_path)
    for entry in manifest["files"]:
        if entry["relative_filename"] == "market_quotes.parquet":
            entry["sha256"] = digest
            entry["byte_count"] = byte_count
            entry["row_count"] = len(rows)
    if "row_counts" in manifest:
        manifest["row_counts"]["market_quotes"] = len(rows)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_digest = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    with connect_database(database) as connection:
        connection.execute(
            "UPDATE snapshots SET checksum_sha256 = ?, row_count = ? WHERE id = ?",
            (manifest_digest, len(rows), snapshot_id),
        )
        connection.execute(
            "UPDATE bookmaker_snapshot_registrations SET checksum_sha256 = ? WHERE snapshot_id = ?",
            (manifest_digest, snapshot_id),
        )
        connection.commit()
        with pytest.raises(
            SnapshotVerificationError,
            match="quote_observation_id must be unique|checksum mismatch",
        ):
            load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )

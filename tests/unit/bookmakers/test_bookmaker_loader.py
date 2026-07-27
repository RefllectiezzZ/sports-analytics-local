"""Strict bookmaker snapshot loader tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.loader import load_bookmaker_snapshot
from sports_analytics.bookmakers.normalization import normalize_bookmaker_bundles
from sports_analytics.bookmakers.reconciliation import reconcile_bookmaker_bundles
from sports_analytics.bookmakers.snapshots import (
    build_bookmaker_source_version,
    publish_bookmaker_snapshot,
)
from sports_analytics.core.exceptions import SnapshotIntegrityError, SnapshotVerificationError
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER
from sports_analytics.sources.betano.synthetic import parse_betano_synthetic_payloads
from sports_analytics.sources.bookmaker_capture import (
    build_capture_manifest,
    manifest_to_raw_artifact,
    persist_capture_manifest,
)
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "betano"


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
    normalized = normalize_bookmaker_bundles((bundle,), reconciliations=reconciled)
    store = BookmakerRawCaptureStore(raw)
    capture = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content=(FIXTURES / "football.json").read_text(encoding="utf-8"),
        retrieved_at=OBSERVED,
        extension="json",
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

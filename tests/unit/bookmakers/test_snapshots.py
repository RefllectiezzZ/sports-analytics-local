"""Bookmaker snapshot suite registration and offline validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.normalization import (
    NormalizedBookmakerBundle,
    normalize_bookmaker_bundles,
)
from sports_analytics.bookmakers.reconciliation import reconcile_bookmaker_bundles
from sports_analytics.bookmakers.schemas import bookmaker_snapshot_suite
from sports_analytics.bookmakers.snapshots import (
    build_bookmaker_snapshot_spec,
    build_bookmaker_source_version,
    prepare_bookmaker_snapshot,
    publish_bookmaker_snapshot,
)
from sports_analytics.bookmakers.types import (
    BOOKMAKER_SCHEMA_VERSION,
    BOOKMAKER_SNAPSHOT_TYPE,
)
from sports_analytics.core.exceptions import RepositoryError, SnapshotVerificationError
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.snapshots.spec import MANIFEST_FILENAME
from sports_analytics.snapshots.writer import discard_prepared_snapshot
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER
from sports_analytics.sources.betano.parser import parse_betano_payloads

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "betano"
OBSERVED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _normalized_bundle() -> NormalizedBookmakerBundle:
    payload = json.loads((FIXTURES / "football.json").read_text(encoding="utf-8"))
    bundle = parse_betano_payloads(
        [payload],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-snap",
        observed_at_utc=OBSERVED_AT,
        sport="football",
    )
    reconciled = reconcile_bookmaker_bundles((bundle,))
    return normalize_bookmaker_bundles((bundle,), reconciliations=reconciled)


def test_bookmaker_snapshot_suite_registration() -> None:
    suite = bookmaker_snapshot_suite(sport_code="football")
    names = [item.dataset_name for item in suite.descriptors]
    assert suite.primary_dataset_name == "market_quotes"
    assert "acquisition_metadata" in names
    assert "market_quotes" in names
    assert "provider_status" in names
    assert "canonical_events" in names


def _source_version(cycle: str) -> str:
    return build_bookmaker_source_version(
        sport_code="football",
        acquisition_cycle_id=cycle,
        raw_sha256="a" * 64,
    )


def test_build_bookmaker_snapshot_spec_validation() -> None:
    bundle = _normalized_bundle()
    source_version = _source_version("cycle-snap")
    spec = build_bookmaker_snapshot_spec(
        sport_code="football",
        source_version=source_version,
        source_observed_at_utc=OBSERVED_AT,
        bundle=bundle,
    )
    assert spec.identity.snapshot_type == BOOKMAKER_SNAPSHOT_TYPE
    assert spec.identity.schema_version == BOOKMAKER_SCHEMA_VERSION
    assert dict(spec.identity.partition_keys)["sport"] == "football"
    with pytest.raises(RepositoryError, match="sport_code must be non-empty"):
        build_bookmaker_snapshot_spec(
            sport_code="",
            source_version=source_version,
            source_observed_at_utc=OBSERVED_AT,
            bundle=bundle,
        )


def test_publish_bookmaker_snapshot_deterministic_and_tamper_detection(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    snapshots = tmp_path / "snapshots"
    bundle = _normalized_bundle()
    source_version = _source_version("cycle-snap")
    first = publish_bookmaker_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        sport_code="football",
        source_version=source_version,
        source_observed_at_utc=OBSERVED_AT,
        bundle=bundle,
    )
    second = publish_bookmaker_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        sport_code="football",
        source_version=source_version,
        source_observed_at_utc=OBSERVED_AT,
        bundle=bundle,
    )
    assert first.published.snapshot_id == second.published.snapshot_id
    assert second.published.snapshot_reused is True
    assert first.published.snapshot_status is SnapshotStatus.READY

    relative_manifest = first.published.snapshot_relative_path
    directory = snapshots / Path(relative_manifest).parent
    assert (directory / MANIFEST_FILENAME).is_file()
    verify_snapshot_directory(
        snapshots_directory=snapshots,
        relative_manifest_path=relative_manifest,
        suite=bookmaker_snapshot_suite(sport_code="football"),
    )

    # Tamper a parquet payload and expect verification failure.
    quote_path = directory / "market_quotes.parquet"
    original = quote_path.read_bytes()
    quote_path.write_bytes(original + b"\x00tamper")
    with pytest.raises(SnapshotVerificationError):
        verify_snapshot_directory(
            snapshots_directory=snapshots,
            relative_manifest_path=relative_manifest,
            suite=bookmaker_snapshot_suite(sport_code="football"),
        )
    quote_path.write_bytes(original)

    # Missing READY/immutable publication surface: remove the manifest marker.
    (directory / MANIFEST_FILENAME).unlink()
    with pytest.raises(SnapshotVerificationError, match="manifest is missing"):
        verify_snapshot_directory(
            snapshots_directory=snapshots,
            relative_manifest_path=relative_manifest,
            suite=bookmaker_snapshot_suite(sport_code="football"),
        )


def test_prepare_bookmaker_snapshot_can_be_discarded(tmp_path: Path) -> None:
    bundle = _normalized_bundle()
    prepared = prepare_bookmaker_snapshot(
        snapshots_directory=tmp_path / "snapshots",
        sport_code="football",
        source_version=_source_version("cycle-prep"),
        source_observed_at_utc=OBSERVED_AT,
        bundle=bundle,
    )
    assert prepared.temporary_directory.exists()
    discard_prepared_snapshot(prepared)
    assert not prepared.temporary_directory.exists()

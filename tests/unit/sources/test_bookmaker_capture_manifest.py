"""Strict URL-free bookmaker capture-manifest v2 tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import PermanentSourceError, SnapshotIntegrityError
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.bookmaker_capture import (
    CAPTURE_MANIFEST_SCHEMA,
    attach_capture_references,
    build_capture_manifest,
    parse_capture_manifest_from_bytes,
    persist_capture_manifest,
    verify_capture_manifest,
)
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderMarketObservation,
    ProviderParticipantObservation,
    ProviderSelectionObservation,
)
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
RELATIVE = "betano-pt/manifests/sha256/aa/" + ("a" * 64) + ".json"


def _document() -> dict[str, object]:
    return {
        "schema": CAPTURE_MANIFEST_SCHEMA,
        "provider_id": "betano-pt",
        "acquisition_cycle_id": "cycle-manifest-v2",
        "captures": [
            {
                "relative_path": "betano-pt/sha256/aa/" + ("a" * 64) + ".json",
                "checksum_sha256": "a" * 64,
                "byte_count": 12,
                "capture_type": "provider-json",
                "observed_at_utc": "2026-08-20T12:00:00.000000Z",
            }
        ],
    }


def _parse(document: dict[str, object]):
    return parse_capture_manifest_from_bytes(
        manifest_bytes=json.dumps(document, sort_keys=True).encode("utf-8"),
        relative_path=RELATIVE,
        expected_provider_id="betano-pt",
        expected_acquisition_cycle_id="cycle-manifest-v2",
    )


def _bundle(*markets: ProviderMarketObservation) -> ProviderAcquisitionBundle:
    return ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="adapter-v1",
        acquisition_cycle_id="cycle-manifest-v2",
        observed_at_utc=NOW,
        sport="football",
        events=(
            ProviderEventObservation(
                source_event_id="event-1",
                source_competition_id="competition-1",
                sport="football",
                scheduled_start_utc=NOW,
                event_state=ProviderEventState.PRE_MATCH,
                participants=(
                    ProviderParticipantObservation("home", "Home", "home"),
                    ProviderParticipantObservation("away", "Away", "away"),
                ),
                markets=markets,
                native_markets=markets,
                source_page_route_id="football-prematch",
            ),
        ),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )


def _market(source_market_id: str, capture_id: str | None) -> ProviderMarketObservation:
    return ProviderMarketObservation(
        source_market_id=source_market_id,
        display_label=source_market_id,
        market_status=MarketStatus.OPEN,
        selections=(
            ProviderSelectionObservation(
                source_selection_id=f"{source_market_id}-selection",
                display_label="Selection",
                decimal_odds=Decimal("2.00"),
                selection_status=SelectionStatus.ACTIVE,
                source_capture_id=capture_id,
            ),
        ),
        source_capture_id=capture_id,
    )


def test_valid_v2_manifest_parses_without_url_fields() -> None:
    parsed = _parse(_document())
    assert parsed.schema == "bookmaker-capture-manifest-v2"
    assert len(parsed.entries) == 1
    assert "url" not in parsed.manifest_bytes.decode("utf-8").casefold()


@pytest.mark.parametrize(
    "unknown_key",
    ["source_url", "response_url", "url", "unexpected_field"],
)
def test_unknown_or_url_entry_fields_are_rejected(unknown_key: str) -> None:
    document = _document()
    captures = document["captures"]
    assert isinstance(captures, list)
    entry = captures[0]
    assert isinstance(entry, dict)
    entry[unknown_key] = "fake"
    with pytest.raises(SnapshotIntegrityError, match="entry 0 keys"):
        _parse(document)


def test_unknown_root_field_is_rejected() -> None:
    document = _document()
    document["unexpected_root"] = "fake"
    with pytest.raises(SnapshotIntegrityError, match="root keys"):
        _parse(document)


def test_legacy_v1_is_explicitly_rejected() -> None:
    document = _document()
    document["schema"] = "bookmaker-capture-manifest-v1"
    with pytest.raises(SnapshotIntegrityError, match="legacy capture manifest v1"):
        _parse(document)


def test_v2_checksum_persistence_verification_and_reload(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    capture = BookmakerRawCaptureStore(raw).store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"synthetic":true}',
        retrieved_at=NOW,
        extension="json",
    )
    manifest = persist_capture_manifest(
        raw_directory=raw,
        manifest=build_capture_manifest(
            provider_id="betano-pt",
            acquisition_cycle_id="cycle-manifest-v2",
            captures=(capture,),
        ),
    )
    verify_capture_manifest(raw_directory=raw, manifest=manifest)
    reloaded = parse_capture_manifest_from_bytes(
        manifest_bytes=(raw / manifest.relative_path).read_bytes(),
        relative_path=manifest.relative_path,
        expected_provider_id="betano-pt",
        expected_acquisition_cycle_id="cycle-manifest-v2",
    )
    assert reloaded.checksum_sha256 == manifest.checksum_sha256
    assert reloaded.entries == manifest.entries


def test_single_capture_defaults_missing_row_provenance(tmp_path: Path) -> None:
    capture = BookmakerRawCaptureStore(tmp_path).store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":1}',
        retrieved_at=NOW,
        extension="json",
    )
    attached = attach_capture_references(_bundle(_market("market-1", None)), (capture,))
    event = attached.events[0]
    assert event.source_capture_ids == (capture.checksum_sha256,)
    assert event.native_markets[0].source_capture_id == capture.checksum_sha256
    assert event.native_markets[0].selections[0].source_capture_id == capture.checksum_sha256


def test_multi_capture_keeps_exact_event_subset_and_row_provenance(tmp_path: Path) -> None:
    store = BookmakerRawCaptureStore(tmp_path)
    first = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":1}',
        retrieved_at=NOW,
        extension="json",
    )
    second = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":2}',
        retrieved_at=NOW,
        extension="json",
    )
    unrelated = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":3}',
        retrieved_at=NOW,
        extension="json",
    )
    attached = attach_capture_references(
        _bundle(
            _market("market-1", first.checksum_sha256),
            _market("market-2", second.checksum_sha256),
        ),
        (first, second, unrelated),
    )
    event = attached.events[0]
    assert event.source_capture_ids == tuple(
        sorted((first.checksum_sha256, second.checksum_sha256))
    )
    assert unrelated.checksum_sha256 not in event.source_capture_ids
    assert event.completeness.source_responses_contributing == 2


@pytest.mark.parametrize("unknown", [False, True])
def test_multi_capture_missing_or_unknown_row_provenance_fails_closed(
    tmp_path: Path,
    unknown: bool,
) -> None:
    store = BookmakerRawCaptureStore(tmp_path)
    first = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":1}',
        retrieved_at=NOW,
        extension="json",
    )
    second = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":2}',
        retrieved_at=NOW,
        extension="json",
    )
    capture_id = "f" * 64 if unknown else None
    with pytest.raises(PermanentSourceError, match="missing or unknown"):
        attach_capture_references(
            _bundle(_market("market-1", capture_id)),
            (first, second),
        )


def test_multi_capture_selection_provenance_is_independently_verified(
    tmp_path: Path,
) -> None:
    store = BookmakerRawCaptureStore(tmp_path)
    first = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":1}',
        retrieved_at=NOW,
        extension="json",
    )
    second = store.store_text(
        source_name="betano-pt",
        capture_kind="provider-json",
        content='{"capture":2}',
        retrieved_at=NOW,
        extension="json",
    )
    market = _market("market-1", first.checksum_sha256)
    market = replace(
        market,
        selections=(replace(market.selections[0], source_capture_id=None),),
    )
    with pytest.raises(PermanentSourceError, match="selection provenance"):
        attach_capture_references(_bundle(market), (first, second))

"""Verified Betano topEventsV2 football extraction profile tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.bookmakers.loader import load_verified_bookmaker_quotes
from sports_analytics.bookmakers.service import BookmakerIngestionService
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.markets.contracts import MarketStatus
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION, PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.betano_topeventsv2 import (
    BETANO_FOOTBALL_TOPEVENTSV2_PROFILE,
    BETANO_TOPEVENTSV2_PROFILE_ID,
    looks_like_topeventsv2,
    translate_topeventsv2,
)
from sports_analytics.sources.bookmaker_extraction.pipeline import apply_extraction_profile
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserMode,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import RecordingBrowserSession

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "betano"
    / "topeventsv2_football_sanitized.json"
)


def _raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _punctuated_competition_body() -> str:
    raw = _raw()
    top_events = raw["data"]["topEventsV2"]
    top_events["leagues"]["synth-league-1"]["name"] = "(Taça D'Água / Norte!!!)"
    top_events["leagues"]["synth-league-2"] = {
        "id": "synth-league-2",
        "name": "...Copa D'Oeste///",
    }
    top_events["events"]["synth-event-beta"]["leagueId"] = "synth-league-2"
    return json.dumps(raw)


def test_punctuated_competition_construction_uses_real_utf8() -> None:
    raw = json.loads(_punctuated_competition_body())
    leagues = raw["data"]["topEventsV2"]["leagues"]
    assert leagues["synth-league-1"]["name"] == "(Taça D'Água / Norte!!!)"
    assert leagues["synth-league-2"]["name"] == "...Copa D'Oeste///"


def _browser(body: str, *, observed_at: datetime = OBSERVED) -> BrowserAcquisitionResult:
    return BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="cycle-topeventsv2",
        observed_at_utc=observed_at,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(
            BrowserResponseObservation(
                provider_id=PROVIDER_ID,
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/synth/topevents",
                observed_at_utc=observed_at,
                content_type="application/json",
                body_text=body,
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )


def test_real_shape_recognition() -> None:
    assert looks_like_topeventsv2(_raw()) is True
    assert looks_like_topeventsv2({"data": {"other": {}}}) is False


def test_two_football_events_and_market_mappings() -> None:
    translated = translate_topeventsv2(_raw(), observed_at_utc=OBSERVED)
    events = translated["payload"]["events"]
    assert len(events) == 2
    assert {event["eventId"] for event in events} == {
        "synth-event-alpha",
        "synth-event-beta",
    }
    alpha = next(event for event in events if event["eventId"] == "synth-event-alpha")
    codes = {market["marketTypeCode"] for market in alpha["markets"]}
    assert codes == {"MRES", "TOTG", "BTTS"}
    total = next(market for market in alpha["markets"] if market["marketTypeCode"] == "TOTG")
    assert total["line"] == "2.5"
    assert {sel["name"] for sel in total["selections"]} == {"over", "under"}
    mres = next(market for market in alpha["markets"] if market["marketTypeCode"] == "MRES")
    assert [sel["name"] for sel in mres["selections"]] == ["1", "X", "2"]
    btts = next(market for market in alpha["markets"] if market["marketTypeCode"] == "BTTS")
    assert {sel["name"] for sel in btts["selections"]} == {"yes", "no"}
    beta = next(event for event in events if event["eventId"] == "synth-event-beta")
    beta_total = next(market for market in beta["markets"] if market["marketTypeCode"] == "TOTG")
    assert beta_total["line"] == "3.5"
    suspended = [market for market in beta["markets"] if market["status"] == "SUSPENDED"]
    assert len(suspended) == 1


def test_sparse_suspension_absent_means_open() -> None:
    translated = translate_topeventsv2(_raw(), observed_at_utc=OBSERVED)
    alpha = translated["payload"]["events"][0]
    assert all(
        market["status"] == "OPEN"
        for market in alpha["markets"]
        if market["marketTypeCode"] in {"MRES", "TOTG", "BTTS"}
    )


def test_live_and_non_football_excluded() -> None:
    translated = translate_topeventsv2(_raw(), observed_at_utc=OBSERVED)
    drifts = set(translated["drift_codes"])
    assert "live-event-excluded" in drifts
    assert "non-football-excluded" in drifts
    assert "malformed-event-reference" in drifts
    assert "malformed-market-reference" in drifts
    assert "unknown-market" in drifts


def test_structural_drift_missing_top_events() -> None:
    body = json.dumps({"data": {"topEventsV2": {"events": {}}}})
    result = BETANO_FOOTBALL_TOPEVENTSV2_PROFILE.extract(
        browser_result=_browser(body),
        captures=(),
    )
    assert result.adapter_contract_payloads == ()
    assert (
        "no-topeventsv2-payload" in result.drift_codes
        or "topeventsv2-rejected" in result.drift_codes
    )


def test_label_mismatch_rejects_selection() -> None:
    raw = _raw()
    raw["data"]["topEventsV2"]["selections"]["synth-sel-alpha-home"]["name"] = "WRONG"
    translated = translate_topeventsv2(raw, observed_at_utc=OBSERVED)
    # Event may be dropped if market mapping fails closed.
    assert "selection-mapping-rejected" in set(translated["drift_codes"]) or not any(
        event["eventId"] == "synth-event-alpha" for event in translated["payload"]["events"]
    )


def test_verified_profile_registered() -> None:
    profile = get_verified_extraction_profile(PROVIDER_ID)
    assert profile is not None
    assert profile.verified is True
    assert profile.profile_id == BETANO_TOPEVENTSV2_PROFILE_ID
    assert get_verified_extraction_profile("betclic-pt") is None


def test_pipeline_produces_prematch_events_without_invented_rules() -> None:
    browser = _browser(FIXTURE.read_text(encoding="utf-8"))
    bundle = apply_extraction_profile(
        profile=BETANO_FOOTBALL_TOPEVENTSV2_PROFILE,
        browser_result=browser,
        captures=(),
        adapter_version=ADAPTER_VERSION,
        parser_version="betano-pt-parser-v1",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    assert len(bundle.events) == 2
    for event in bundle.events:
        assert len(event.participants) == 2
        for market in event.markets:
            assert market.rules_scope is None
            assert market.overtime_scope is None
            if market.market_status is MarketStatus.SUSPENDED:
                continue
            assert market.selections


def test_adapter_uses_response_observation_timestamps(tmp_path: Path) -> None:
    from sports_analytics.sources.betano.adapter import acquire_betano_current_odds

    cycle_start = OBSERVED
    response_time = OBSERVED + timedelta(seconds=17)
    body = FIXTURE.read_text(encoding="utf-8")
    acquisition = BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="ts-cycle",
        observed_at_utc=cycle_start,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(
            BrowserResponseObservation(
                provider_id=PROVIDER_ID,
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/synth/topevents",
                observed_at_utc=response_time,
                content_type="application/json",
                body_text=body,
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    session = RecordingBrowserSession(acquisition)
    _browser_result, bundle, captures = acquire_betano_current_odds(
        sport="football",
        acquisition_cycle_id="ts-cycle",
        observed_at_utc=cycle_start,
        raw_directory=tmp_path / "raw",
        session=session,
    )
    assert captures[0].retrieved_at == response_time
    assert bundle.observed_at_utc == response_time
    assert len(bundle.events) == 2


def test_end_to_end_publish_and_strict_reload(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "db.sqlite3")
    body = _punctuated_competition_body()
    observed_1 = OBSERVED
    observed_2 = OBSERVED + timedelta(minutes=5)
    result_1 = BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="smoke-betano-pt-football-1",
        observed_at_utc=observed_1,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(
            BrowserResponseObservation(
                provider_id=PROVIDER_ID,
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/synth/topevents",
                observed_at_utc=observed_1,
                content_type="application/json",
                body_text=body,
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    result_2 = BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="smoke-betano-pt-football-2",
        observed_at_utc=observed_2,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(
            BrowserResponseObservation(
                provider_id=PROVIDER_ID,
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/synth/topevents",
                observed_at_utc=observed_2,
                content_type="application/json",
                body_text=body,
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )

    class _SequencedSession:
        def __init__(self, results: list[BrowserAcquisitionResult]) -> None:
            self._results = list(results)
            self.calls = 0

        def acquire(self, **kwargs):
            index = min(self.calls, len(self._results) - 1)
            self.calls += 1
            base = self._results[index]
            return BrowserAcquisitionResult(
                provider_id=base.provider_id,
                sport=base.sport,
                acquisition_cycle_id=str(
                    kwargs.get("acquisition_cycle_id", base.acquisition_cycle_id)
                ),
                observed_at_utc=base.observed_at_utc,
                browser_mode=base.browser_mode,
                pages=base.pages,
                responses=base.responses,
                diagnostics=base.diagnostics,
                block_reason=base.block_reason,
                warnings=base.warnings,
                network_metadata=getattr(base, "network_metadata", ()),
            )

    service = BookmakerIngestionService(
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        bookmakers=BookmakersSettings(enabled=True),
        clock=lambda: observed_1,
        session=_SequencedSession([result_1]),
    )
    first = service.ingest(
        provider_id=PROVIDER_ID,
        sport="football",
        observed_at_utc=observed_1,
        acquisition_cycle_id="e2e-betano-1",
    )
    assert first.status == "succeeded"
    assert first.snapshot_id is not None
    assert first.events_observed >= 2
    assert first.valid_quotes_observed >= 2

    service_2 = BookmakerIngestionService(
        database_path=tmp_path / "db.sqlite3",
        raw_directory=tmp_path / "raw",
        snapshots_directory=tmp_path / "snapshots",
        bookmakers=BookmakersSettings(enabled=True),
        clock=lambda: observed_2,
        session=_SequencedSession([result_2]),
    )
    second = service_2.ingest(
        provider_id=PROVIDER_ID,
        sport="football",
        observed_at_utc=observed_2,
        acquisition_cycle_id="e2e-betano-2",
    )
    assert second.status == "succeeded"
    assert second.snapshot_id is not None

    with connect_database(tmp_path / "db.sqlite3", read_only=True) as connection:
        loaded_1 = load_verified_bookmaker_quotes(
            database_connection=connection,
            snapshots_directory=tmp_path / "snapshots",
            raw_directory=tmp_path / "raw",
            snapshot_id=first.snapshot_id,
        )
        loaded_2 = load_verified_bookmaker_quotes(
            database_connection=connection,
            snapshots_directory=tmp_path / "snapshots",
            raw_directory=tmp_path / "raw",
            snapshot_id=second.snapshot_id,
        )
    assert loaded_1.verified is True
    assert loaded_2.verified is True
    assert loaded_1.event_count >= 2
    assert loaded_1.catalogue is not None
    event_ids = {
        quote.identity.canonical_event_id for _, quote in loaded_1.verified_quotes_by_observation_id
    }
    assert len(event_ids) == loaded_1.event_count
    # Quotes remain non-comparable until rules evidence is reviewed.
    assert all(not quote.comparable for _, quote in loaded_1.verified_quotes_by_observation_id)
    manifests = list((tmp_path / "raw" / PROVIDER_ID / "manifests").rglob("*.json"))
    assert manifests
    manifest_text = manifests[0].read_text(encoding="utf-8")
    assert "source_url" not in manifest_text
    assert "response_url" not in manifest_text
    manifest_document = json.loads(manifest_text)
    assert manifest_document["schema"] == "bookmaker-capture-manifest-v2"
    assert all(
        "url" not in str(key).casefold() for entry in manifest_document["captures"] for key in entry
    )

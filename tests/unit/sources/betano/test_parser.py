"""Offline Betano fixture-bundle parser tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_analytics.markets.contracts import MarketStatus
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION, PROVIDER_ID
from sports_analytics.sources.betano.parser import parse_betano_acquisition
from sports_analytics.sources.betano.synthetic import parse_betano_synthetic_payloads
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderEventState,
)
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserMode,
    BrowserResponseObservation,
)
from sports_analytics.sources.browser.playwright_runtime import (
    build_structural_page_observation,
)
from sports_analytics.sources.browser.safety import classify_block_signals

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "betano"
OBSERVED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    loaded = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _parse(payload: dict[str, Any], *, sport: str = "football") -> ProviderAcquisitionBundle:
    return parse_betano_synthetic_payloads(
        [payload],
        provider_id=PROVIDER_ID,
        adapter_version=ADAPTER_VERSION,
        acquisition_cycle_id="cycle-betano-test",
        observed_at_utc=OBSERVED_AT,
        sport=sport,
    )


def test_parses_football_fixture_with_locale_decimals() -> None:
    bundle = _parse(_load("football.json"), sport="football")
    assert len(bundle.events) == 1
    event = bundle.events[0]
    assert event.sport == "football"
    assert event.event_state is ProviderEventState.PRE_MATCH
    market = next(
        item
        for item in event.markets
        if item.canonical_market_definition_id == "football-match-result-1x2"
    )
    odds = {sel.source_selection_id: sel.decimal_odds for sel in market.selections}
    assert odds["betano-sel-home"] == Decimal("1.85")
    assert odds["betano-sel-draw"] == Decimal("3.40")
    assert odds["betano-sel-away"] == Decimal("4.20")


def test_parses_basketball_and_tennis_fixtures() -> None:
    basketball = _parse(_load("basketball.json"), sport="basketball")
    tennis = _parse(_load("tennis.json"), sport="tennis")
    assert basketball.events[0].sport == "basketball"
    assert len(basketball.events[0].markets) == 3
    assert tennis.events[0].sport == "tennis"
    assert tennis.events[0].markets[0].canonical_market_definition_id == "tennis-match-winner"
    assert {p.role for p in tennis.events[0].participants} == {"player-1", "player-2"}


def test_suspended_market_preserved() -> None:
    bundle = _parse(_load("suspended.json"))
    market = bundle.events[0].markets[0]
    assert market.market_status is MarketStatus.SUSPENDED


def test_missing_odds_skipped_keeps_priced_selections() -> None:
    payload = _load("football.json")
    payload["events"][0]["markets"][0]["selections"][0]["decimal_odds"] = None
    bundle = _parse(payload)
    market = next(
        item
        for item in bundle.events[0].markets
        if item.canonical_market_definition_id == "football-match-result-1x2"
    )
    assert "betano-sel-home" not in {sel.source_selection_id for sel in market.selections}
    assert len(market.selections) == 2


def test_duplicate_selection_identities_reject_event() -> None:
    payload = _load("football.json")
    market = payload["events"][0]["markets"][0]
    market["selections"].append(
        {
            "source_selection_id": "betano-sel-home",
            "display_label": "Home Dup",
            "decimal_odds": "2.00",
            "selection_status": "active",
        }
    )
    bundle = _parse(payload)
    assert bundle.events == ()
    assert any(warning.code == "event-rejected" for warning in bundle.warnings)
    assert "duplicate source selection identities" in bundle.warnings[0].message


def test_unknown_markets_retained_for_audit() -> None:
    payload = _load("football.json")
    payload["unknown_markets"] = [{"id": "shots", "label": "Player Shots"}]
    payload["events"][0]["markets"].append(
        {
            "source_market_id": "betano-mkt-unknown",
            "display_label": "Player Shots",
            "market_status": "open",
            "period": "full-match",
            "canonical_market_definition_id": None,
            "selections": [
                {
                    "source_selection_id": "betano-sel-unk",
                    "display_label": "Over 2.5",
                    "decimal_odds": "1.50",
                    "selection_status": "active",
                }
            ],
        }
    )
    bundle = _parse(payload)
    assert "unknown-market" in bundle.drift_codes
    assert any(warning.code == "unknown-market-retained" for warning in bundle.warnings)
    unknown = next(
        item for item in bundle.events[0].markets if item.canonical_market_definition_id is None
    )
    assert unknown.display_label == "Player Shots"


def test_schema_drift_is_reported() -> None:
    bundle = _parse(_load("drift.json"))
    assert bundle.events == ()
    assert "unknown-schema" in bundle.drift_codes
    assert any(warning.code == "schema-drift" for warning in bundle.warnings)


def test_block_classification_is_fixed_before_parser_boundary() -> None:
    blocked = _load("blocked.json")
    reason = classify_block_signals(
        title=blocked["page_signals"]["title"],
        body_text=blocked["page_signals"]["body_text"],
    )
    assert reason is BrowserBlockReason.CAPTCHA

    page = build_structural_page_observation(
        provider_id=PROVIDER_ID,
        page_route_id="football-prematch",
        final_url="https://www.betano.pt/sport/futebol/",
        observed_at_utc=OBSERVED_AT,
        allowed_hostnames=frozenset({"www.betano.pt"}),
        title=blocked["page_signals"]["title"],
        body_html=None,
        body_text=blocked["page_signals"]["body_text"],
    )
    assert page.block_reason is BrowserBlockReason.CAPTCHA
    acquisition = BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="cycle-blocked",
        observed_at_utc=OBSERVED_AT,
        browser_mode=BrowserMode.VISIBLE,
        pages=(page,),
        responses=(),
        diagnostics=(),
        block_reason=page.block_reason,
        warnings=(),
    )
    bundle = parse_betano_acquisition(acquisition, adapter_version=ADAPTER_VERSION)
    assert any(warning.code == "provider-blocked" for warning in bundle.warnings)


def test_provider_block_reason_short_circuits_parsing() -> None:
    acquisition = BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="cycle-block-reason",
        observed_at_utc=OBSERVED_AT,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(
            BrowserResponseObservation(
                provider_id=PROVIDER_ID,
                page_route_id="football-prematch",
                response_url="https://www.betano.pt/api/synthetic",
                observed_at_utc=OBSERVED_AT,
                content_type="application/json",
                body_text=json.dumps(_load("football.json")),
                status_code=200,
                warnings=(),
            ),
        ),
        diagnostics=(),
        block_reason=BrowserBlockReason.ACCESS_DENIED,
        warnings=(),
    )
    bundle = parse_betano_acquisition(acquisition, adapter_version=ADAPTER_VERSION)
    assert bundle.events == ()
    assert any(warning.code == "provider-blocked" for warning in bundle.warnings)


def test_live_event_is_rejected() -> None:
    payload = _load("football.json")
    payload["events"][0]["event_state"] = "live"
    bundle = _parse(payload)
    assert bundle.events == ()
    assert any("live events are not supported" in warning.message for warning in bundle.warnings)


def test_utc_timestamp_parsing() -> None:
    bundle = _parse(_load("football.json"))
    assert bundle.events[0].scheduled_start_utc == datetime(2026, 8, 15, 18, 0, tzinfo=UTC)

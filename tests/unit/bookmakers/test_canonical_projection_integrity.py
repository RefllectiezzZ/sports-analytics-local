"""Reviewed canonical-outcome and active-price projection invariants."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.normalization import normalize_bookmaker_bundles
from sports_analytics.bookmakers.reconciliation import reconcile_bookmaker_bundles
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER
from sports_analytics.sources.betano.synthetic import parse_betano_synthetic_payloads
from sports_analytics.sources.betclic.catalog import ADAPTER_VERSION as BETCLIC_ADAPTER
from sports_analytics.sources.betclic.synthetic import parse_betclic_synthetic_payloads
from sports_analytics.sources.bookmaker_contracts import (
    CanonicalOutcomeKey,
    ProviderAcquisitionBundle,
    ProviderSelectionPriceState,
)

OBSERVED = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _payload(provider: str) -> dict:
    return json.loads((FIXTURES / provider / "football.json").read_text(encoding="utf-8"))


def _betano_bundle(payload: dict | None = None) -> ProviderAcquisitionBundle:
    return parse_betano_synthetic_payloads(
        [payload or _payload("betano")],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-canonical-projection",
        observed_at_utc=OBSERVED,
        sport="football",
    )


def _normalized(bundle: ProviderAcquisitionBundle):
    return normalize_bookmaker_bundles(
        (bundle,),
        reconciliations=reconcile_bookmaker_bundles((bundle,)),
    )


def _replace_first_market(
    bundle: ProviderAcquisitionBundle,
    *,
    market_status: MarketStatus | None = None,
    first_selection_changes: dict[str, object] | None = None,
) -> ProviderAcquisitionBundle:
    event = bundle.events[0]
    market = event.markets[0]
    first = replace(market.selections[0], **(first_selection_changes or {}))
    market = replace(
        market,
        market_status=market_status or market.market_status,
        selections=(first, *market.selections[1:]),
    )
    return replace(bundle, events=(replace(event, markets=(market, *event.markets[1:])),))


def test_unknown_label_with_valid_source_identity_remains_native_only() -> None:
    payload = _payload("betano")
    raw_selection = payload["events"][0]["markets"][0]["selections"][0]
    raw_selection["display_label"] = "Mystery outcome"
    raw_selection["source_selection_id"] = "valid-provider-selection-999"

    bundle = _betano_bundle(payload)
    native_selection = bundle.events[0].markets[0].selections[0]
    normalized = _normalized(bundle)

    assert native_selection.source_selection_id == "valid-provider-selection-999"
    assert native_selection.canonical_outcome_key is None
    assert {quote.selection.outcome_key for quote in normalized.market_quotes} == {
        "draw",
        "away",
        "over",
        "under",
        "yes",
        "no",
    }
    assert len(normalized.comparison_eligibility) == len(normalized.market_quotes)
    assert all(
        item.canonical_selection_id != "valid-provider-selection-999"
        for item in normalized.comparison_eligibility
    )


def test_source_selection_identity_does_not_change_reviewed_semantics() -> None:
    original = _betano_bundle()
    changed = _replace_first_market(
        original,
        first_selection_changes={"source_selection_id": "provider-rotated-home-id"},
    )

    original_home = _normalized(original).market_quotes[0].selection.outcome_key
    changed_outcomes = {quote.selection.outcome_key for quote in _normalized(changed).market_quotes}

    assert original_home in changed_outcomes
    assert "provider-rotated-home-id" not in changed_outcomes


def test_provider_specific_ids_share_the_same_reviewed_outcomes() -> None:
    betano = _betano_bundle()
    betclic = parse_betclic_synthetic_payloads(
        [_payload("betclic")],
        provider_id="betclic-pt",
        adapter_version=BETCLIC_ADAPTER,
        acquisition_cycle_id="cycle-canonical-projection",
        observed_at_utc=OBSERVED,
        sport="football",
    )

    betano_outcomes = {quote.selection.outcome_key for quote in _normalized(betano).market_quotes}
    betclic_outcomes = {quote.selection.outcome_key for quote in _normalized(betclic).market_quotes}
    expected = {item.value for item in CanonicalOutcomeKey}
    assert betano_outcomes == expected
    assert betclic_outcomes == expected
    assert (
        betano.events[0].markets[0].selections[0].source_selection_id
        != betclic.events[0].markets[0].selections[0].source_selection_id
    )


@pytest.mark.parametrize(
    ("market_status", "selection_status", "price_state", "priced", "expected_quotes"),
    [
        (MarketStatus.OPEN, SelectionStatus.ACTIVE, ProviderSelectionPriceState.PRICED, True, 7),
        (MarketStatus.OPEN, SelectionStatus.ACTIVE, ProviderSelectionPriceState.UNPRICED, False, 6),
        (
            MarketStatus.OPEN,
            SelectionStatus.SUSPENDED,
            ProviderSelectionPriceState.PRICED,
            True,
            6,
        ),
        (
            MarketStatus.OPEN,
            SelectionStatus.SUSPENDED,
            ProviderSelectionPriceState.UNPRICED,
            False,
            6,
        ),
        (
            MarketStatus.CLOSED,
            SelectionStatus.ACTIVE,
            ProviderSelectionPriceState.PRICED,
            True,
            4,
        ),
    ],
)
def test_only_active_priced_selections_in_open_markets_project_canonical_quotes(
    market_status: MarketStatus,
    selection_status: SelectionStatus,
    price_state: ProviderSelectionPriceState,
    priced: bool,
    expected_quotes: int,
) -> None:
    bundle = _replace_first_market(
        _betano_bundle(),
        market_status=market_status,
        first_selection_changes={
            "selection_status": selection_status,
            "price_state": price_state,
            "decimal_odds": (
                _betano_bundle().events[0].markets[0].selections[0].decimal_odds if priced else None
            ),
        },
    )
    normalized = _normalized(bundle)
    assert len(normalized.market_quotes) == expected_quotes
    assert len(normalized.comparison_eligibility) == expected_quotes
    assert all(
        quote.selection_status == SelectionStatus.ACTIVE.value for quote in normalized.market_quotes
    )
    assert all(quote.market_status == MarketStatus.OPEN.value for quote in normalized.market_quotes)


def test_open_market_mixture_excludes_suspended_selection_from_comparison() -> None:
    original = _betano_bundle()
    event = original.events[0]
    market = event.markets[0]
    suspended = replace(market.selections[1], selection_status=SelectionStatus.SUSPENDED)
    mixed_market = replace(
        market,
        selections=(market.selections[0], suspended, market.selections[2]),
    )
    bundle = replace(
        original,
        events=(replace(event, markets=(mixed_market, *event.markets[1:])),),
    )
    normalized = _normalized(bundle)
    assert len(normalized.market_quotes) == 6
    assert "draw" not in {quote.selection.outcome_key for quote in normalized.market_quotes}
    assert "draw" not in {item.canonical_selection_id for item in normalized.comparison_eligibility}

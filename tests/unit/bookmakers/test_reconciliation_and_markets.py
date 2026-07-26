"""Reconciliation and market-equivalence offline coverage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sports_analytics.bookmakers.markets import (
    DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
    DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
    DEFINITION_FOOTBALL_TOTAL_GOALS,
    UnknownProviderMarket,
    map_provider_market_to_canonical,
)
from sports_analytics.bookmakers.reconciliation import (
    participant_identity_scope_for_sport,
    reconcile_bookmaker_bundles,
)
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER
from sports_analytics.sources.betano.parser import parse_betano_payloads
from sports_analytics.sources.betclic.catalog import ADAPTER_VERSION as BETCLIC_ADAPTER
from sports_analytics.sources.betclic.parser import parse_betclic_payloads
from sports_analytics.sources.bookmaker_contracts import (
    ProviderMarketObservation,
    ProviderSelectionObservation,
)
from sports_analytics.sports.contracts import ReconciliationState
from sports_analytics.sports.identifiers import SPORT_FOOTBALL

OBSERVED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
BETANO_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "betano"
BETCLIC_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "betclic"


def _selection(source_id: str, label: str, odds: str) -> ProviderSelectionObservation:
    return ProviderSelectionObservation(
        source_selection_id=source_id,
        display_label=label,
        decimal_odds=Decimal(odds),
        selection_status=SelectionStatus.ACTIVE,
    )


def test_participant_identity_scope_is_competition_independent() -> None:
    assert participant_identity_scope_for_sport(SPORT_FOOTBALL) == "club:portugal"
    # Same scope for football regardless of competition-specific route.
    assert participant_identity_scope_for_sport("football") == "club:portugal"


def test_cross_provider_exact_event_reconciliation() -> None:
    betano = parse_betano_payloads(
        [json.loads((BETANO_FIXTURES / "football.json").read_text(encoding="utf-8"))],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-a",
        observed_at_utc=OBSERVED_AT,
        sport="football",
    )
    betclic_payload = json.loads((BETCLIC_FIXTURES / "football.json").read_text(encoding="utf-8"))
    # Align competition display name and kickoff for exact compatibility.
    betclic_payload["events"][0]["competition_display_name"] = "Synthetic Portugal Liga"
    betclic_payload["events"][0]["scheduled_start_utc"] = "2026-08-15T18:05:00Z"
    betclic_payload["events"][0]["participants"] = [
        {
            "source_participant_id": "betclic-club-northbridge",
            "display_name": "Northbridge FC",
            "role": "home",
            "normalized_name": "northbridge fc",
        },
        {
            "source_participant_id": "betclic-club-southport",
            "display_name": "Southport Athletic",
            "role": "away",
            "normalized_name": "southport athletic",
        },
    ]
    betclic = parse_betclic_payloads(
        [betclic_payload],
        provider_id="betclic-pt",
        adapter_version=BETCLIC_ADAPTER,
        acquisition_cycle_id="cycle-b",
        observed_at_utc=OBSERVED_AT,
        sport="football",
    )
    reconciled = reconcile_bookmaker_bundles((betano, betclic), start_tolerance_seconds=900)
    resolved = [
        item
        for item in reconciled.event_reconciliations
        if item.state == ReconciliationState.EXACT.value
    ]
    assert resolved
    canonical_ids = {item.canonical_event_id for item in resolved if item.canonical_event_id}
    assert len(canonical_ids) == 1


def test_incompatible_competitions_do_not_merge() -> None:
    betano = parse_betano_payloads(
        [json.loads((BETANO_FIXTURES / "football.json").read_text(encoding="utf-8"))],
        provider_id="betano-pt",
        adapter_version=BETANO_ADAPTER,
        acquisition_cycle_id="cycle-a",
        observed_at_utc=OBSERVED_AT,
        sport="football",
    )
    betclic_payload = json.loads((BETCLIC_FIXTURES / "football.json").read_text(encoding="utf-8"))
    betclic_payload["events"][0]["competition_display_name"] = "Other Cup Synthetic"
    betclic_payload["events"][0]["source_competition_id"] = "other-cup"
    betclic = parse_betclic_payloads(
        [betclic_payload],
        provider_id="betclic-pt",
        adapter_version=BETCLIC_ADAPTER,
        acquisition_cycle_id="cycle-b",
        observed_at_utc=OBSERVED_AT,
        sport="football",
    )
    reconciled = reconcile_bookmaker_bundles((betano, betclic), start_tolerance_seconds=900)
    by_source = {
        (item.source_name, item.source_event_id): item for item in reconciled.event_reconciliations
    }
    left = by_source[("betano-pt", betano.events[0].source_event_id)]
    right = by_source[("betclic-pt", betclic.events[0].source_event_id)]
    if left.canonical_event_id and right.canonical_event_id:
        assert left.canonical_event_id != right.canonical_event_id


def test_market_equivalence_requires_exact_period_line_and_rules() -> None:
    base = ProviderMarketObservation(
        source_market_id="m1",
        display_label="Total Goals",
        market_status=MarketStatus.OPEN,
        selections=(
            _selection("s1", "Over", "1.90"),
            _selection("s2", "Under", "1.95"),
        ),
        period="full-match",
        line=Decimal("2.5"),
        canonical_market_definition_id=DEFINITION_FOOTBALL_TOTAL_GOALS,
    )
    mapped = map_provider_market_to_canonical(base)
    assert not isinstance(mapped, UnknownProviderMarket)
    assert mapped.definition.line_value == Decimal("2.5")

    wrong_period = ProviderMarketObservation(
        source_market_id="m2",
        display_label="Total Goals 1H",
        market_status=MarketStatus.OPEN,
        selections=base.selections,
        period="first-half",
        line=Decimal("2.5"),
        canonical_market_definition_id=DEFINITION_FOOTBALL_TOTAL_GOALS,
    )
    assert isinstance(map_provider_market_to_canonical(wrong_period), UnknownProviderMarket)

    wrong_line = ProviderMarketObservation(
        source_market_id="m3",
        display_label="Total Goals",
        market_status=MarketStatus.OPEN,
        selections=base.selections,
        period="full-match",
        line=Decimal("3.5"),
        canonical_market_definition_id=DEFINITION_FOOTBALL_TOTAL_GOALS,
    )
    mapped_wrong_line = map_provider_market_to_canonical(wrong_line)
    assert not isinstance(mapped_wrong_line, UnknownProviderMarket)
    assert mapped_wrong_line.definition.line_value != mapped.definition.line_value

    one_x2 = ProviderMarketObservation(
        source_market_id="m4",
        display_label="1X2",
        market_status=MarketStatus.OPEN,
        selections=(
            _selection("h", "Home", "1.80"),
            _selection("d", "Draw", "3.40"),
            _selection("a", "Away", "4.20"),
        ),
        period="full-match",
        canonical_market_definition_id=DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
    )
    assert not isinstance(map_provider_market_to_canonical(one_x2), UnknownProviderMarket)

    # Basketball OT markets require overtime_scope exactness.
    missing_ot = ProviderMarketObservation(
        source_market_id="m5",
        display_label="Winner",
        market_status=MarketStatus.OPEN,
        selections=(
            _selection("h", "Home", "1.70"),
            _selection("a", "Away", "2.10"),
        ),
        period="full-match",
        overtime_scope=None,
        canonical_market_definition_id=DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
    )
    assert isinstance(map_provider_market_to_canonical(missing_ot), UnknownProviderMarket)

"""Same-bookmaker multiple invariant and cross-bookmaker singles tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.multiples import (
    BookmakerMultipleLeg,
    CrossBookmakerSinglesComparison,
    RequestedMultipleLegSpec,
    build_cross_bookmaker_singles_comparison,
    build_same_bookmaker_multiple,
    compare_provider_multiples,
)
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    QuoteSelectionReason,
)
from sports_analytics.bookmakers.verified_evidence import VerifiedBookmakerQuote
from sports_analytics.core.exceptions import PermanentSourceError
from tests.unit.bookmakers.verified_quote_helpers import verified_quote

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
MAX_AGE = 300


def _leg(bookmaker_id: str, leg_key: str, odds: str) -> BookmakerMultipleLeg:
    return BookmakerMultipleLeg(
        bookmaker_id=bookmaker_id,
        canonical_event_id=f"event-{leg_key}",
        canonical_market_definition_id="football-match-result-1x2",
        canonical_selection_id="home",
        decimal_odds=Decimal(odds),
        observed_at_utc=NOW,
        leg_key=leg_key,
    )


def _quote(provider_id: str, leg_key: str, odds: str) -> VerifiedBookmakerQuote:
    return verified_quote(
        provider_id=provider_id,
        odds=odds,
        leg_key=leg_key,
        observed_at=NOW,
    )


def test_duplicate_canonical_identities_rejected_across_leg_keys() -> None:
    specs = (
        RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
        RequestedMultipleLegSpec("b", "event-a", "football-match-result-1x2", "home"),
    )
    with pytest.raises(PermanentSourceError, match="duplicate canonical bet identities"):
        compare_provider_multiples(
            specs,
            {},
            {},
            evaluated_at_utc=NOW,
            quote_maximum_age_seconds=MAX_AGE,
        )


def test_same_bookmaker_multiple_builds_product_total() -> None:
    multiple = build_same_bookmaker_multiple(
        [_leg(PROVIDER_BETANO_PT, "a", "1.50"), _leg(PROVIDER_BETANO_PT, "b", "2.00")]
    )
    assert multiple.bookmaker_id == PROVIDER_BETANO_PT
    assert multiple.total_decimal_odds == Decimal("3.00")
    assert all(leg.bookmaker_id == PROVIDER_BETANO_PT for leg in multiple.legs)


def test_mixed_provider_legs_rejected_as_multiple() -> None:
    with pytest.raises(PermanentSourceError, match="mixed-provider"):
        build_same_bookmaker_multiple(
            [_leg(PROVIDER_BETANO_PT, "a", "1.50"), _leg(PROVIDER_BETCLIC_PT, "b", "2.00")]
        )


def test_provider_totals_calculated_separately_and_best_complete_selected() -> None:
    specs = (
        RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
        RequestedMultipleLegSpec("b", "event-b", "football-match-result-1x2", "home"),
    )
    comparison = compare_provider_multiples(
        specs,
        {
            "a": _quote(PROVIDER_BETANO_PT, "a", "1.50"),
            "b": _quote(PROVIDER_BETANO_PT, "b", "2.00"),
        },
        {
            "a": _quote(PROVIDER_BETCLIC_PT, "a", "1.60"),
            "b": _quote(PROVIDER_BETCLIC_PT, "b", "2.10"),
        },
        evaluated_at_utc=NOW,
        quote_maximum_age_seconds=MAX_AGE,
    )
    assert comparison.betano_eligible and comparison.betclic_eligible
    assert comparison.betano_multiple is not None
    assert comparison.betclic_multiple is not None
    assert comparison.betano_multiple.total_decimal_odds == Decimal("3.00")
    assert comparison.betclic_multiple.total_decimal_odds == Decimal("3.36")
    assert comparison.selected_multiple is comparison.betclic_multiple
    assert comparison.reason_code is QuoteSelectionReason.HIGHER_ODDS


def test_equal_complete_totals_select_betano() -> None:
    specs = (
        RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
        RequestedMultipleLegSpec("b", "event-b", "football-match-result-1x2", "home"),
    )
    comparison = compare_provider_multiples(
        specs,
        {
            "a": _quote(PROVIDER_BETANO_PT, "a", "1.50"),
            "b": _quote(PROVIDER_BETANO_PT, "b", "2.00"),
        },
        {
            "a": _quote(PROVIDER_BETCLIC_PT, "a", "1.50"),
            "b": _quote(PROVIDER_BETCLIC_PT, "b", "2.00"),
        },
        evaluated_at_utc=NOW,
        quote_maximum_age_seconds=MAX_AGE,
    )
    assert comparison.selected_multiple is comparison.betano_multiple
    assert comparison.reason_code is QuoteSelectionReason.EQUAL_ODDS_PREFERRED


def test_incomplete_provider_coverage_is_ineligible() -> None:
    specs = (
        RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
        RequestedMultipleLegSpec("b", "event-b", "football-match-result-1x2", "home"),
    )
    comparison = compare_provider_multiples(
        specs,
        {
            "a": _quote(PROVIDER_BETANO_PT, "a", "1.50"),
            "b": _quote(PROVIDER_BETANO_PT, "b", "2.00"),
        },
        {"a": _quote(PROVIDER_BETCLIC_PT, "a", "1.80")},
        evaluated_at_utc=NOW,
        quote_maximum_age_seconds=MAX_AGE,
    )
    assert comparison.betano_eligible is True
    assert comparison.betclic_eligible is False
    assert comparison.selected_multiple is comparison.betano_multiple
    assert comparison.reason_code is QuoteSelectionReason.PREFERRED_ONLY


def test_cross_bookmaker_singles_comparison_is_not_a_multiple() -> None:
    betano = _quote(PROVIDER_BETANO_PT, "a", "1.50").to_priced_quote(
        evaluated_at_utc=NOW,
        maximum_age_seconds=MAX_AGE,
    )
    betclic = _quote(PROVIDER_BETCLIC_PT, "b", "2.00").to_priced_quote(
        evaluated_at_utc=NOW,
        maximum_age_seconds=MAX_AGE,
    )
    comparison = build_cross_bookmaker_singles_comparison(
        leg_keys=("a", "b"),
        betano_quotes_by_leg_key={"a": betano},
        betclic_quotes_by_leg_key={"b": betclic},
    )
    assert isinstance(comparison, CrossBookmakerSinglesComparison)
    assert not hasattr(comparison, "total_decimal_odds")
    assert comparison.reason_code is QuoteSelectionReason.BOTH_RETAINED
    assert comparison.__class__.__name__ != "BookmakerMultiple"


def test_stale_quote_makes_provider_ineligible() -> None:
    specs = (
        RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
        RequestedMultipleLegSpec("b", "event-b", "football-match-result-1x2", "home"),
    )
    comparison = compare_provider_multiples(
        specs,
        {
            "a": verified_quote(
                provider_id=PROVIDER_BETANO_PT,
                odds="1.50",
                leg_key="a",
                observed_at=NOW,
                age_seconds=9999,
            ),
            "b": verified_quote(
                provider_id=PROVIDER_BETANO_PT,
                odds="2.00",
                leg_key="b",
                observed_at=NOW,
                age_seconds=9999,
            ),
        },
        {
            "a": _quote(PROVIDER_BETCLIC_PT, "a", "1.60"),
            "b": _quote(PROVIDER_BETCLIC_PT, "b", "2.10"),
        },
        evaluated_at_utc=NOW,
        quote_maximum_age_seconds=MAX_AGE,
    )
    assert comparison.betano_eligible is False
    assert comparison.betclic_eligible is True

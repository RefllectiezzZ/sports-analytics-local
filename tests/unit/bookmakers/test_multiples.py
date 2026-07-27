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
from sports_analytics.bookmakers.selection import BookmakerPricedQuote
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    QuoteSelectionReason,
)
from sports_analytics.core.exceptions import PermanentSourceError

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


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


def _quote(
    provider_id: str,
    leg_key: str,
    odds: str,
    *,
    fresh: bool = True,
    snapshot_id: str = "snap-1",
    snapshot_checksum_sha256: str = "a" * 64,
) -> BookmakerPricedQuote:
    return BookmakerPricedQuote(
        provider_id=provider_id,
        decimal_odds=Decimal(odds),
        observed_at_utc=NOW,
        canonical_event_id=f"event-{leg_key}",
        canonical_market_definition_id="football-match-result-1x2",
        canonical_selection_id="home",
        fresh=fresh,
        snapshot_id=snapshot_id,
        snapshot_checksum_sha256=snapshot_checksum_sha256,
    )


def test_duplicate_canonical_identities_rejected_across_leg_keys() -> None:
    specs = (
        RequestedMultipleLegSpec("a", "event-a", "football-match-result-1x2", "home"),
        RequestedMultipleLegSpec("b", "event-a", "football-match-result-1x2", "home"),
    )
    with pytest.raises(PermanentSourceError, match="duplicate canonical bet identities"):
        compare_provider_multiples(specs, {}, {})


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
    )
    assert comparison.betano_eligible is True
    assert comparison.betclic_eligible is False
    assert comparison.selected_multiple is comparison.betano_multiple
    assert comparison.reason_code is QuoteSelectionReason.PREFERRED_ONLY


def test_cross_bookmaker_singles_comparison_is_not_a_multiple() -> None:
    comparison = build_cross_bookmaker_singles_comparison(
        leg_keys=("a", "b"),
        betano_quotes_by_leg_key={"a": _quote(PROVIDER_BETANO_PT, "a", "1.50")},
        betclic_quotes_by_leg_key={"b": _quote(PROVIDER_BETCLIC_PT, "b", "2.00")},
    )
    assert isinstance(comparison, CrossBookmakerSinglesComparison)
    assert not hasattr(comparison, "total_decimal_odds")
    assert comparison.reason_code is QuoteSelectionReason.BOTH_RETAINED
    assert comparison.__class__.__name__ != "BookmakerMultiple"

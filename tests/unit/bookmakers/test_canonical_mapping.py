"""Canonical market definition mapping tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sports_analytics.bookmakers.canonical_mapping import (
    canonical_market_definition_id_from_quote_dimensions,
)
from sports_analytics.bookmakers.markets import (
    DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
    DEFINITION_FOOTBALL_TOTAL_GOALS,
)
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.markets.identifiers import build_market_key


def test_football_1x2_market_key_maps_exactly() -> None:
    market_key = build_market_key(
        sport_code="football",
        market_family="match-result",
        variant="1x2",
        market_period="full-match",
    )
    definition_id = canonical_market_definition_id_from_quote_dimensions(
        market_key=market_key,
        line_type="none",
        line_value=None,
        market_period="full-match",
    )
    assert definition_id == DEFINITION_FOOTBALL_MATCH_RESULT_1X2
    assert definition_id == "football-match-result-1x2"


def test_football_total_requires_line() -> None:
    market_key = build_market_key(
        sport_code="football",
        market_family="totals",
        variant="goals",
        market_period="full-match",
    )
    definition_id = canonical_market_definition_id_from_quote_dimensions(
        market_key=market_key,
        line_type="total",
        line_value=Decimal("2.5"),
        market_period="full-match",
    )
    assert definition_id == DEFINITION_FOOTBALL_TOTAL_GOALS


def test_basketball_total_and_spread_require_exact_line_types() -> None:
    from sports_analytics.bookmakers.markets import (
        DEFINITION_BASKETBALL_SPREAD_WITH_OT,
        DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
    )

    total_key = build_market_key(
        sport_code="basketball",
        market_family="totals",
        variant="points-with-ot",
        market_period="full-match",
    )
    spread_key = build_market_key(
        sport_code="basketball",
        market_family="spread",
        variant="with-ot",
        market_period="full-match",
    )
    assert (
        canonical_market_definition_id_from_quote_dimensions(
            market_key=total_key,
            line_type="total",
            line_value=Decimal("210.5"),
            market_period="full-match",
        )
        == DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT
    )
    assert (
        canonical_market_definition_id_from_quote_dimensions(
            market_key=spread_key,
            line_type="spread",
            line_value=Decimal("-3.5"),
            market_period="full-match",
        )
        == DEFINITION_BASKETBALL_SPREAD_WITH_OT
    )


def test_unknown_rules_make_quote_non_comparable() -> None:
    from sports_analytics.bookmakers.canonical_mapping import quote_is_comparable

    assert (
        quote_is_comparable(
            definition_id=DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
            overtime_scope=None,
            rules_scope=None,
        )
        is False
    )
    assert (
        quote_is_comparable(
            definition_id=DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
            overtime_scope=None,
            rules_scope="regulation-only",
        )
        is True
    )


def test_total_line_mismatch_rejected() -> None:
    market_key = build_market_key(
        sport_code="football",
        market_family="totals",
        variant="goals",
        market_period="full-match",
    )
    with pytest.raises(PermanentSourceError, match="requires line_type total"):
        canonical_market_definition_id_from_quote_dimensions(
            market_key=market_key,
            line_type="none",
            line_value=None,
            market_period="full-match",
        )

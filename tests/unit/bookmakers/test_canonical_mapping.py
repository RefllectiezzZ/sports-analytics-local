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


def test_total_line_mismatch_rejected() -> None:
    market_key = build_market_key(
        sport_code="football",
        market_family="totals",
        variant="goals",
        market_period="full-match",
    )
    with pytest.raises(PermanentSourceError, match="requires total or spread line_type"):
        canonical_market_definition_id_from_quote_dimensions(
            market_key=market_key,
            line_type="none",
            line_value=None,
            market_period="full-match",
        )

"""Exact reviewed mapping from persisted quote dimensions to canonical market IDs."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from sports_analytics.bookmakers.markets import (
    DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
    DEFINITION_BASKETBALL_SPREAD_WITH_OT,
    DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
    DEFINITION_FOOTBALL_BTTS,
    DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
    DEFINITION_FOOTBALL_TOTAL_GOALS,
    DEFINITION_TENNIS_MATCH_WINNER,
    MARKET_PERIOD_FULL_MATCH,
)
from sports_analytics.core.exceptions import PermanentSourceError, SnapshotVerificationError
from sports_analytics.markets.contracts import LineType
from sports_analytics.markets.identifiers import build_market_key
from sports_analytics.sports.identifiers import SPORT_BASKETBALL, SPORT_FOOTBALL, SPORT_TENNIS

CANONICAL_MAPPING_VERSION: Final[str] = "bookmaker-canonical-mapping-v1"

_EXACT_MARKET_KEY_DEFINITIONS: Final[dict[str, str]] = {
    build_market_key(
        sport_code=SPORT_FOOTBALL,
        market_family="match-result",
        variant="1x2",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
    build_market_key(
        sport_code=SPORT_FOOTBALL,
        market_family="both-teams-to-score",
        variant="yes-no",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_FOOTBALL_BTTS,
    build_market_key(
        sport_code=SPORT_FOOTBALL,
        market_family="totals",
        variant="goals",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_FOOTBALL_TOTAL_GOALS,
    build_market_key(
        sport_code=SPORT_BASKETBALL,
        market_family="match-result",
        variant="winner-with-ot",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
    build_market_key(
        sport_code=SPORT_BASKETBALL,
        market_family="totals",
        variant="points-with-ot",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
    build_market_key(
        sport_code=SPORT_BASKETBALL,
        market_family="spread",
        variant="with-ot",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_BASKETBALL_SPREAD_WITH_OT,
    build_market_key(
        sport_code=SPORT_TENNIS,
        market_family="match-result",
        variant="winner",
        market_period=MARKET_PERIOD_FULL_MATCH,
    ): DEFINITION_TENNIS_MATCH_WINNER,
}

_REQUIRED_LINE_TYPE: Final[dict[str, str]] = {
    DEFINITION_FOOTBALL_MATCH_RESULT_1X2: LineType.NONE.value,
    DEFINITION_FOOTBALL_BTTS: LineType.NONE.value,
    DEFINITION_FOOTBALL_TOTAL_GOALS: LineType.TOTAL.value,
    DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT: LineType.NONE.value,
    DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT: LineType.TOTAL.value,
    DEFINITION_BASKETBALL_SPREAD_WITH_OT: LineType.SPREAD.value,
    DEFINITION_TENNIS_MATCH_WINNER: LineType.NONE.value,
}


def canonical_market_definition_id_from_quote_dimensions(
    *,
    market_key: str,
    line_type: str,
    line_value: Decimal | None,
    market_period: str,
) -> str:
    """Map persisted quote dimensions to one exact canonical market definition ID."""
    if market_period != MARKET_PERIOD_FULL_MATCH:
        msg = f"unsupported market period for exact mapping: {market_period}"
        raise PermanentSourceError(msg)
    definition_id = _EXACT_MARKET_KEY_DEFINITIONS.get(market_key)
    if definition_id is None:
        msg = f"unknown market_key for canonical mapping: {market_key}"
        raise PermanentSourceError(msg)
    expected_line = _REQUIRED_LINE_TYPE[definition_id]
    if line_type != expected_line:
        msg = f"{definition_id} requires line_type {expected_line}"
        raise PermanentSourceError(msg)
    if expected_line == LineType.NONE.value:
        if line_value is not None:
            msg = f"{definition_id} must not carry a line_value"
            raise PermanentSourceError(msg)
    elif line_value is None:
        msg = f"{definition_id} requires an explicit line_value"
        raise PermanentSourceError(msg)
    return definition_id


def quote_is_comparable(
    *,
    definition_id: str,
    overtime_scope: str | None,
    rules_scope: str | None,
) -> bool:
    """Return whether provider-supplied scopes make a quote cross-comparable.

    Scopes must come from provider evidence. Missing/unknown rules make the quote
    non-comparable. Overtime-included basketball definitions require overtime
    evidence.
    """
    if rules_scope is None or not str(rules_scope).strip():
        return False
    if definition_id in {
        DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
        DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
        DEFINITION_BASKETBALL_SPREAD_WITH_OT,
    }:
        if overtime_scope is None or not str(overtime_scope).strip():
            return False
    return True


def canonical_market_definition_id_from_row(row: dict[str, object]) -> str:
    """Derive canonical market definition ID from one verified quote row."""
    market_key = str(row["market_key"])
    line_type = str(row["line_type"])
    line_value_raw = row.get("line_value")
    line_value = None if line_value_raw is None else Decimal(str(line_value_raw))
    market_period = str(row["market_period"])
    try:
        return canonical_market_definition_id_from_quote_dimensions(
            market_key=market_key,
            line_type=line_type,
            line_value=line_value,
            market_period=market_period,
        )
    except PermanentSourceError as exc:
        raise SnapshotVerificationError(str(exc)) from exc

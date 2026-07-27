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
    OVERTIME_INCLUDED,
    REGULATION_ONLY,
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
    if definition_id in {
        DEFINITION_FOOTBALL_TOTAL_GOALS,
        DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
        DEFINITION_BASKETBALL_SPREAD_WITH_OT,
    }:
        if line_type not in {LineType.TOTAL.value, LineType.SPREAD.value}:
            msg = f"{definition_id} requires total or spread line_type"
            raise PermanentSourceError(msg)
        if line_value is None:
            msg = f"{definition_id} requires an explicit line_value"
            raise PermanentSourceError(msg)
    elif line_type != LineType.NONE.value:
        msg = f"{definition_id} requires line_type none"
        raise PermanentSourceError(msg)
    return definition_id


def overtime_scope_from_definition_id(definition_id: str) -> str | None:
    """Return overtime scope implied by a canonical market definition ID."""
    if definition_id in {
        DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
        DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
        DEFINITION_BASKETBALL_SPREAD_WITH_OT,
    }:
        return OVERTIME_INCLUDED
    return None


def rules_scope_from_definition_id(definition_id: str) -> str | None:
    """Return rules scope implied by a canonical market definition ID."""
    if definition_id in {
        DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
        DEFINITION_FOOTBALL_TOTAL_GOALS,
        DEFINITION_FOOTBALL_BTTS,
    }:
        return REGULATION_ONLY
    if definition_id in {
        DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
        DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
        DEFINITION_BASKETBALL_SPREAD_WITH_OT,
    }:
        return OVERTIME_INCLUDED
    return None


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

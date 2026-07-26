"""Canonical bookmaker market definitions and exact provider mappings.

Markets are mapped only when a provider observation carries an exact known
``canonical_market_definition_id``. Vague display labels alone never invent
equivalence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sports_analytics.core.exceptions import NormalizationError, PermanentSourceError
from sports_analytics.markets.contracts import (
    LineType,
    MarketDefinition,
    MarketSelection,
    ParticipantScope,
    validate_line_value,
)
from sports_analytics.markets.identifiers import build_market_key
from sports_analytics.sources.bookmaker_contracts import ProviderMarketObservation
from sports_analytics.sports.identifiers import (
    SPORT_BASKETBALL,
    SPORT_FOOTBALL,
    SPORT_TENNIS,
)

MARKET_PERIOD_FULL_MATCH: Final[str] = "full-match"
OVERTIME_INCLUDED: Final[str] = "overtime-included"
REGULATION_ONLY: Final[str] = "regulation-only"

DEFINITION_FOOTBALL_MATCH_RESULT_1X2: Final[str] = "football-match-result-1x2"
DEFINITION_FOOTBALL_TOTAL_GOALS: Final[str] = "football-total-goals"
DEFINITION_FOOTBALL_BTTS: Final[str] = "football-btts"
DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT: Final[str] = "basketball-match-winner-with-ot"
DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT: Final[str] = "basketball-total-points-with-ot"
DEFINITION_BASKETBALL_SPREAD_WITH_OT: Final[str] = "basketball-spread-with-ot"
DEFINITION_TENNIS_MATCH_WINNER: Final[str] = "tennis-match-winner"

OUTCOME_HOME: Final[str] = "home"
OUTCOME_DRAW: Final[str] = "draw"
OUTCOME_AWAY: Final[str] = "away"
OUTCOME_OVER: Final[str] = "over"
OUTCOME_UNDER: Final[str] = "under"
OUTCOME_YES: Final[str] = "yes"
OUTCOME_NO: Final[str] = "no"

KNOWN_CANONICAL_MARKET_DEFINITION_IDS: Final[frozenset[str]] = frozenset(
    {
        DEFINITION_FOOTBALL_MATCH_RESULT_1X2,
        DEFINITION_FOOTBALL_TOTAL_GOALS,
        DEFINITION_FOOTBALL_BTTS,
        DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT,
        DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT,
        DEFINITION_BASKETBALL_SPREAD_WITH_OT,
        DEFINITION_TENNIS_MATCH_WINNER,
    }
)

FOOTBALL_1X2_OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_HOME, OUTCOME_DRAW, OUTCOME_AWAY)
TOTAL_OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_OVER, OUTCOME_UNDER)
BTTS_OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_YES, OUTCOME_NO)
MATCH_WINNER_OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_HOME, OUTCOME_AWAY)
SPREAD_OUTCOMES: Final[tuple[str, ...]] = (OUTCOME_HOME, OUTCOME_AWAY)


@dataclass(frozen=True, slots=True)
class CanonicalMarketMappingResult:
    """Exact mapping of one provider market onto a canonical definition."""

    definition_id: str
    definition: MarketDefinition
    overtime_scope: str | None
    rules_metadata: tuple[tuple[str, str], ...]
    unknown: bool = False


@dataclass(frozen=True, slots=True)
class UnknownProviderMarket:
    """Auditable retention of a provider market that cannot be mapped exactly."""

    source_market_id: str
    display_label: str
    canonical_market_definition_id: str | None
    reason: str


def football_match_result_1x2_definition() -> MarketDefinition:
    """Return the canonical full-match football 1X2 definition."""
    return MarketDefinition(
        sport_code=SPORT_FOOTBALL,
        market_family="match-result",
        market_key=build_market_key(
            sport_code=SPORT_FOOTBALL,
            market_family="match-result",
            variant="1x2",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.NONE.value,
        line_value=None,
        canonical_participant_id=None,
    )


def football_total_goals_definition(*, line: Decimal) -> MarketDefinition:
    """Return the canonical full-match football total-goals definition."""
    return MarketDefinition(
        sport_code=SPORT_FOOTBALL,
        market_family="totals",
        market_key=build_market_key(
            sport_code=SPORT_FOOTBALL,
            market_family="totals",
            variant="goals",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.TOTAL.value,
        line_value=validate_line_value(line),
        canonical_participant_id=None,
    )


def football_btts_definition() -> MarketDefinition:
    """Return the canonical both-teams-to-score definition."""
    return MarketDefinition(
        sport_code=SPORT_FOOTBALL,
        market_family="both-teams-to-score",
        market_key=build_market_key(
            sport_code=SPORT_FOOTBALL,
            market_family="both-teams-to-score",
            variant="yes-no",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.NONE.value,
        line_value=None,
        canonical_participant_id=None,
    )


def basketball_match_winner_with_ot_definition() -> MarketDefinition:
    """Return basketball match-winner including overtime."""
    return MarketDefinition(
        sport_code=SPORT_BASKETBALL,
        market_family="match-result",
        market_key=build_market_key(
            sport_code=SPORT_BASKETBALL,
            market_family="match-result",
            variant="winner-with-ot",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.NONE.value,
        line_value=None,
        canonical_participant_id=None,
    )


def basketball_total_points_with_ot_definition(*, line: Decimal) -> MarketDefinition:
    """Return basketball full-match points total including overtime."""
    return MarketDefinition(
        sport_code=SPORT_BASKETBALL,
        market_family="totals",
        market_key=build_market_key(
            sport_code=SPORT_BASKETBALL,
            market_family="totals",
            variant="points-with-ot",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.TOTAL.value,
        line_value=validate_line_value(line),
        canonical_participant_id=None,
    )


def basketball_spread_with_ot_definition(*, line: Decimal) -> MarketDefinition:
    """Return basketball full-match spread including overtime."""
    return MarketDefinition(
        sport_code=SPORT_BASKETBALL,
        market_family="spread",
        market_key=build_market_key(
            sport_code=SPORT_BASKETBALL,
            market_family="spread",
            variant="with-ot",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.SPREAD.value,
        line_value=validate_line_value(line),
        canonical_participant_id=None,
    )


def tennis_match_winner_definition(
    *,
    best_of: int | None = None,
    retirement_rule: str | None = None,
) -> tuple[MarketDefinition, tuple[tuple[str, str], ...]]:
    """Return tennis match-winner definition plus optional equivalence metadata."""
    metadata: list[tuple[str, str]] = []
    if best_of is not None:
        if best_of not in {3, 5}:
            msg = "tennis best_of must be 3 or 5 when provided"
            raise PermanentSourceError(msg)
        metadata.append(("best-of", str(best_of)))
    if retirement_rule is not None:
        if not retirement_rule.strip():
            msg = "tennis retirement_rule must be non-empty when provided"
            raise PermanentSourceError(msg)
        metadata.append(("retirement-rule", retirement_rule.strip()))
    definition = MarketDefinition(
        sport_code=SPORT_TENNIS,
        market_family="match-result",
        market_key=build_market_key(
            sport_code=SPORT_TENNIS,
            market_family="match-result",
            variant="winner",
            market_period=MARKET_PERIOD_FULL_MATCH,
        ),
        market_period=MARKET_PERIOD_FULL_MATCH,
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.NONE.value,
        line_value=None,
        canonical_participant_id=None,
    )
    return definition, tuple(sorted(metadata))


def canonical_selection(
    definition: MarketDefinition,
    *,
    outcome_key: str,
    source_market_id: str | None = None,
    source_selection_id: str | None = None,
) -> MarketSelection:
    """Build one canonical market selection with optional source provenance."""
    return MarketSelection(
        definition=definition,
        outcome_key=outcome_key,
        source_market_id=source_market_id,
        source_selection_id=source_selection_id,
    )


def map_provider_market_to_canonical(
    market: ProviderMarketObservation,
) -> CanonicalMarketMappingResult | UnknownProviderMarket:
    """Map a provider market only when its definition ID is exactly known.

    Missing or unknown ``canonical_market_definition_id`` values are recorded as
    unknown rather than inferred from display labels.
    """
    definition_id = market.canonical_market_definition_id
    if definition_id is None:
        return UnknownProviderMarket(
            source_market_id=market.source_market_id,
            display_label=market.display_label,
            canonical_market_definition_id=None,
            reason="missing canonical_market_definition_id",
        )
    if definition_id not in KNOWN_CANONICAL_MARKET_DEFINITION_IDS:
        return UnknownProviderMarket(
            source_market_id=market.source_market_id,
            display_label=market.display_label,
            canonical_market_definition_id=definition_id,
            reason=f"unknown canonical_market_definition_id: {definition_id}",
        )

    try:
        definition, overtime_scope, rules_metadata = _build_definition(
            definition_id=definition_id,
            line=market.line,
            overtime_scope=market.overtime_scope,
            rules_scope=market.rules_scope,
            period=market.period,
        )
    except (NormalizationError, PermanentSourceError) as exc:
        return UnknownProviderMarket(
            source_market_id=market.source_market_id,
            display_label=market.display_label,
            canonical_market_definition_id=definition_id,
            reason=str(exc),
        )

    return CanonicalMarketMappingResult(
        definition_id=definition_id,
        definition=definition,
        overtime_scope=overtime_scope,
        rules_metadata=rules_metadata,
        unknown=False,
    )


def _build_definition(
    *,
    definition_id: str,
    line: Decimal | None,
    overtime_scope: str | None,
    rules_scope: str | None,
    period: str | None,
) -> tuple[MarketDefinition, str | None, tuple[tuple[str, str], ...]]:
    if period is not None and period != MARKET_PERIOD_FULL_MATCH:
        msg = f"unsupported market period for exact mapping: {period}"
        raise PermanentSourceError(msg)

    if definition_id == DEFINITION_FOOTBALL_MATCH_RESULT_1X2:
        return football_match_result_1x2_definition(), None, ()

    if definition_id == DEFINITION_FOOTBALL_TOTAL_GOALS:
        if line is None:
            msg = "football-total-goals requires an explicit line"
            raise PermanentSourceError(msg)
        return football_total_goals_definition(line=line), None, ()

    if definition_id == DEFINITION_FOOTBALL_BTTS:
        return football_btts_definition(), None, ()

    if definition_id == DEFINITION_BASKETBALL_MATCH_WINNER_WITH_OT:
        _require_overtime_included(overtime_scope)
        return basketball_match_winner_with_ot_definition(), OVERTIME_INCLUDED, ()

    if definition_id == DEFINITION_BASKETBALL_TOTAL_POINTS_WITH_OT:
        if line is None:
            msg = "basketball-total-points-with-ot requires an explicit line"
            raise PermanentSourceError(msg)
        _require_overtime_included(overtime_scope)
        return basketball_total_points_with_ot_definition(line=line), OVERTIME_INCLUDED, ()

    if definition_id == DEFINITION_BASKETBALL_SPREAD_WITH_OT:
        if line is None:
            msg = "basketball-spread-with-ot requires an explicit line"
            raise PermanentSourceError(msg)
        _require_overtime_included(overtime_scope)
        return basketball_spread_with_ot_definition(line=line), OVERTIME_INCLUDED, ()

    if definition_id == DEFINITION_TENNIS_MATCH_WINNER:
        best_of: int | None = None
        retirement_rule: str | None = None
        if rules_scope:
            for part in rules_scope.split(";"):
                token = part.strip()
                if token.startswith("best-of:"):
                    best_of = int(token.split(":", 1)[1])
                elif token.startswith("retirement:"):
                    retirement_rule = token.split(":", 1)[1]
        definition, metadata = tennis_match_winner_definition(
            best_of=best_of,
            retirement_rule=retirement_rule,
        )
        return definition, None, metadata

    msg = f"unhandled canonical market definition id: {definition_id}"
    raise PermanentSourceError(msg)


def _require_overtime_included(overtime_scope: str | None) -> None:
    if overtime_scope is None:
        msg = "basketball overtime-included markets require overtime_scope"
        raise PermanentSourceError(msg)
    if overtime_scope not in {OVERTIME_INCLUDED, "with-ot", "ot-included"}:
        msg = f"basketball market overtime_scope is not overtime-included: {overtime_scope}"
        raise PermanentSourceError(msg)

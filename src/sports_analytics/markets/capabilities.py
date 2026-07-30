"""Deterministic sport and canonical market capability matrix."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class CapabilityState(StrEnum):
    """Exact analytical or product support state."""

    SUPPORTED = "supported"
    DATA_INSUFFICIENT = "data-insufficient"
    MODEL_UNAVAILABLE = "model-unavailable"
    LIVE_STATE_REQUIRED = "live-state-required"
    PLAYER_DATA_REQUIRED = "player-data-required"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class MarketCapability:
    """One row of the public market-capability matrix."""

    sport_code: str
    market_family: str
    required_data: str
    model_family: str
    probability_state: CapabilityState
    fair_odds_state: CapabilityState
    offered_price_state: CapabilityState
    opportunity_state: CapabilityState
    combination_state: CapabilityState
    limitation: str | None = None


_FOOTBALL_SCORE_FAMILIES: Final[tuple[str, ...]] = (
    "match-result",
    "double-chance",
    "draw-no-bet",
    "correct-score",
    "total-goals",
    "both-teams-to-score",
    "team-total-goals",
    "total-goals-odd-even",
    "winning-margin",
    "european-handicap",
    "result-and-total-goals",
    "result-and-btts",
    "double-chance-and-total-goals",
    "double-chance-and-btts",
    "result-or-total-goals",
    "result-or-btts",
    "btts-or-total-goals",
)

_FIRST_HALF_FAMILIES: Final[tuple[str, ...]] = (
    "first-half-result",
    "first-half-totals",
    "first-half-btts",
    "first-half-correct-score",
)

_SETTLEMENT_UNAVAILABLE: Final[tuple[str, ...]] = (
    "asian-handicap",
    "asian-total-goals",
)

_CORNERS_AND_SHOTS: Final[tuple[str, ...]] = (
    "corner-totals",
    "team-corner-totals",
    "corner-handicap",
    "team-most-corners",
    "first-half-corner-totals",
    "shots",
    "shots-on-target",
    "team-shots",
    "team-shots-on-target",
)

_LIVE_FAMILIES: Final[tuple[str, ...]] = (
    "next-goal",
    "next-corner",
)

_PLAYER_FAMILIES: Final[tuple[str, ...]] = (
    "scorer",
    "player-shots",
    "player-shots-on-target",
)


def market_capability_matrix() -> tuple[MarketCapability, ...]:
    """Return the canonical, deterministic support matrix."""
    rows: list[MarketCapability] = []
    for family in _FOOTBALL_SCORE_FAMILIES:
        rows.append(
            MarketCapability(
                sport_code="football",
                market_family=family,
                required_data="finished full-time scores and pre-match team identity",
                model_family="dynamic-poisson-or-dixon-coles",
                probability_state=CapabilityState.SUPPORTED,
                fair_odds_state=CapabilityState.SUPPORTED,
                offered_price_state=CapabilityState.SUPPORTED,
                opportunity_state=CapabilityState.SUPPORTED,
                combination_state=CapabilityState.SUPPORTED,
                limitation=(
                    "same-event placement additionally requires one real offered "
                    "combined-selection price"
                ),
            )
        )
    for family in _FIRST_HALF_FAMILIES:
        rows.append(
            _unavailable(
                sport="football",
                family=family,
                state=CapabilityState.MODEL_UNAVAILABLE,
                required_data="complete timestamp-safe first-half score history",
                limitation="no independently evaluated first-half score champion",
            )
        )
    for family in _SETTLEMENT_UNAVAILABLE:
        rows.append(
            _unavailable(
                sport="football",
                family=family,
                state=CapabilityState.MODEL_UNAVAILABLE,
                required_data="joint score distribution and five-state split-stake settlement",
                limitation=(
                    "current fair-price contract cannot represent win, half-win, push, "
                    "half-loss, and loss without approximation"
                ),
            )
        )
    for family in _CORNERS_AND_SHOTS:
        rows.append(
            _unavailable(
                sport="football",
                family=family,
                state=CapabilityState.DATA_INSUFFICIENT,
                required_data="complete leakage-safe team count history",
                limitation="secondary count model was not promoted in this pass",
            )
        )
    for family in _LIVE_FAMILIES:
        rows.append(
            _unavailable(
                sport="football",
                family=family,
                state=CapabilityState.LIVE_STATE_REQUIRED,
                required_data="verified chronological in-play state",
                limitation="pre-match score distributions do not supply live state",
            )
        )
    for family in _PLAYER_FAMILIES:
        rows.append(
            _unavailable(
                sport="football",
                family=family,
                state=CapabilityState.PLAYER_DATA_REQUIRED,
                required_data="canonical player, availability, role, and expected minutes",
                limitation="team score distributions cannot price player selections",
            )
        )
    for sport, future_model in (
        ("basketball", "possessions-and-efficiency-score-generator"),
        ("tennis", "serve-point-games-sets-generator"),
    ):
        rows.append(
            MarketCapability(
                sport_code=sport,
                market_family="all",
                required_data="sport-specific historical events, features, rules, and evaluation",
                model_family=future_model,
                probability_state=CapabilityState.MODEL_UNAVAILABLE,
                fair_odds_state=CapabilityState.MODEL_UNAVAILABLE,
                offered_price_state=CapabilityState.SUPPORTED,
                opportunity_state=CapabilityState.MODEL_UNAVAILABLE,
                combination_state=CapabilityState.MODEL_UNAVAILABLE,
                limitation="analysis-unavailable: no-sport-specific-model",
            )
        )
    return tuple(sorted(rows, key=lambda item: (item.sport_code, item.market_family)))


def capability_for(sport_code: str, market_family: str) -> MarketCapability:
    """Return an exact family capability, including sport-wide unavailable rows."""
    rows = market_capability_matrix()
    for row in rows:
        if row.sport_code == sport_code and row.market_family == market_family:
            return row
    for row in rows:
        if row.sport_code == sport_code and row.market_family == "all":
            return row
    return _unavailable(
        sport=sport_code,
        family=market_family,
        state=CapabilityState.UNSUPPORTED,
        required_data="reviewed sport-specific market semantics",
        limitation="market family is not in the reviewed capability registry",
    )


def _unavailable(
    *,
    sport: str,
    family: str,
    state: CapabilityState,
    required_data: str,
    limitation: str,
) -> MarketCapability:
    return MarketCapability(
        sport_code=sport,
        market_family=family,
        required_data=required_data,
        model_family="none",
        probability_state=state,
        fair_odds_state=state,
        offered_price_state=CapabilityState.SUPPORTED,
        opportunity_state=state,
        combination_state=state,
        limitation=limitation,
    )

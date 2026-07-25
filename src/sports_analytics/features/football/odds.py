"""Football-specific odds normalization helpers."""

from __future__ import annotations

from sports_analytics.evaluation.metrics import decimal_odds_to_normalized_probabilities
from sports_analytics.features.football.specification import FOOTBALL_1X2_OUTCOME_SPACE


def closing_1x2_odds_to_normalized_probabilities(
    home_odds: float,
    draw_odds: float,
    away_odds: float,
) -> tuple[float, float, float]:
    """Convert a complete closing 1X2 decimal-odds triple to normalized probabilities."""
    home, draw, away = decimal_odds_to_normalized_probabilities(
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        decimal_odds={
            "home": home_odds,
            "draw": draw_odds,
            "away": away_odds,
        },
    )
    return home, draw, away

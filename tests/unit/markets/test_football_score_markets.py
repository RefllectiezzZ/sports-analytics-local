from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from sports_analytics.markets.football_score_markets import (
    ScorePredicateKind,
    complement,
    conjunction_probability,
    derive_full_time_markets,
    find_market_probability,
    predicate_probability,
    primitive,
    union,
)
from sports_analytics.models.football_scores import joint_score_from_intensities


@pytest.fixture
def surface():
    return joint_score_from_intensities(
        home_intensity=1.7,
        away_intensity=1.0,
        prediction_cutoff=date(2025, 1, 1),
    )


def test_mandatory_market_invariants(surface) -> None:
    markets = derive_full_time_markets(surface)
    result = [item for item in markets if item.market_family == "match-result"]
    assert sum(item.probability for item in result) == pytest.approx(1.0)

    for family in ("total-goals", "both-teams-to-score", "total-goals-odd-even"):
        grouped: dict[Decimal | None, list[float]] = {}
        for item in markets:
            if item.market_family == family:
                grouped.setdefault(item.line_value, []).append(item.probability)
        assert all(sum(values) == pytest.approx(1.0) for values in grouped.values())

    home = find_market_probability(
        markets,
        market_family="match-result",
        outcome_key="home",
    )
    assert home.fair_decimal_odds == pytest.approx(1.0 / home.probability)
    correct = find_market_probability(
        markets,
        market_family="correct-score",
        outcome_key="2-1",
    )
    assert correct.probability == pytest.approx(surface.probability(2, 1))


def test_double_chance_dnb_and_combination_families(surface) -> None:
    markets = derive_full_time_markets(surface)
    home = find_market_probability(
        markets,
        market_family="match-result",
        outcome_key="home",
    )
    draw = find_market_probability(
        markets,
        market_family="match-result",
        outcome_key="draw",
    )
    home_or_draw = find_market_probability(
        markets,
        market_family="double-chance",
        outcome_key="home-or-draw",
    )
    assert home_or_draw.probability == pytest.approx(home.probability + draw.probability)

    dnb = find_market_probability(
        markets,
        market_family="draw-no-bet",
        outcome_key="home",
    )
    assert dnb.push_probability == pytest.approx(draw.probability)
    assert dnb.fair_decimal_odds == pytest.approx((1.0 - draw.probability) / home.probability)
    assert any(item.market_family == "result-and-total-goals" for item in markets)
    assert any(item.market_family == "result-and-btts" for item in markets)


def test_same_event_dependence_is_exact_not_marginal_product(surface) -> None:
    home = primitive(ScorePredicateKind.HOME_WIN)
    over = primitive(ScorePredicateKind.TOTAL_OVER, "2.5")
    joint = conjunction_probability(surface, home, over)
    product = predicate_probability(surface, home) * predicate_probability(surface, over)
    assert joint != pytest.approx(product)
    exact_union = predicate_probability(surface, union(home, over))
    assert exact_union == pytest.approx(
        predicate_probability(surface, home) + predicate_probability(surface, over) - joint
    )
    assert predicate_probability(surface, complement(over)) == pytest.approx(
        1.0 - predicate_probability(surface, over)
    )


def test_european_handicap_three_way_partition_and_score_adjustment(surface) -> None:
    markets = derive_full_time_markets(surface)
    rows = tuple(
        item
        for item in markets
        if item.market_family == "european-handicap"
        and item.participant_scope == "home"
        and item.line_value == Decimal("-1")
    )
    assert len(rows) == 3
    assert sum(item.probability for item in rows) == pytest.approx(1.0)
    adjusted_draw = next(item for item in rows if item.outcome_key == "draw")
    expected = sum(
        probability
        for home_goals, row in enumerate(surface.probabilities)
        for away_goals, probability in enumerate(row)
        if home_goals - 1 == away_goals
    )
    assert adjusted_draw.probability == pytest.approx(expected)

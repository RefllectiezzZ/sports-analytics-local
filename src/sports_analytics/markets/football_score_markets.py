"""Reviewed football market predicates over one coherent score distribution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.markets.identifiers import build_market_key
from sports_analytics.models.football_scores import JointScoreDistribution

FULL_MATCH: Final[str] = "full-match"
HALF_GOAL_LINES: Final[tuple[Decimal, ...]] = tuple(Decimal(f"{whole}.5") for whole in range(6))


class ScorePredicateKind(StrEnum):
    """Closed, non-executable predicate vocabulary."""

    HOME_WIN = "home-win"
    DRAW = "draw"
    AWAY_WIN = "away-win"
    DOUBLE_CHANCE = "double-chance"
    EXACT_SCORE = "exact-score"
    TOTAL_OVER = "total-over"
    TOTAL_UNDER = "total-under"
    TEAM_TOTAL_OVER = "team-total-over"
    TEAM_TOTAL_UNDER = "team-total-under"
    BTTS_YES = "btts-yes"
    BTTS_NO = "btts-no"
    TOTAL_ODD = "total-odd"
    TOTAL_EVEN = "total-even"
    WINNING_MARGIN = "winning-margin"
    EUROPEAN_HANDICAP = "european-handicap"
    AND = "and"
    OR = "or"
    NOT = "not"


@dataclass(frozen=True, slots=True)
class ScorePredicate:
    """One fixed score-state predicate or reviewed composition."""

    kind: ScorePredicateKind
    arguments: tuple[str, ...] = ()
    children: tuple[ScorePredicate, ...] = ()

    def __post_init__(self) -> None:
        primitive = self.kind not in {
            ScorePredicateKind.AND,
            ScorePredicateKind.OR,
            ScorePredicateKind.NOT,
        }
        if primitive and self.children:
            raise EvaluationError("primitive score predicates cannot have children")
        if self.kind in {ScorePredicateKind.AND, ScorePredicateKind.OR}:
            if len(self.children) < 2 or self.arguments:
                raise EvaluationError("AND/OR predicates require at least two children")
        if self.kind is ScorePredicateKind.NOT:
            if len(self.children) != 1 or self.arguments:
                raise EvaluationError("NOT predicates require exactly one child")
        _validate_predicate_arguments(self.kind, self.arguments)

    def matches(self, home_goals: int, away_goals: int) -> bool:
        """Evaluate the closed predicate without dynamic code execution."""
        if self.kind is ScorePredicateKind.HOME_WIN:
            return home_goals > away_goals
        if self.kind is ScorePredicateKind.DRAW:
            return home_goals == away_goals
        if self.kind is ScorePredicateKind.AWAY_WIN:
            return home_goals < away_goals
        if self.kind is ScorePredicateKind.DOUBLE_CHANCE:
            result = (
                "home"
                if home_goals > away_goals
                else "draw"
                if home_goals == away_goals
                else "away"
            )
            return result in self.arguments
        if self.kind is ScorePredicateKind.EXACT_SCORE:
            return home_goals == int(self.arguments[0]) and away_goals == int(self.arguments[1])
        if self.kind is ScorePredicateKind.TOTAL_OVER:
            return home_goals + away_goals > float(self.arguments[0])
        if self.kind is ScorePredicateKind.TOTAL_UNDER:
            return home_goals + away_goals < float(self.arguments[0])
        if self.kind in {
            ScorePredicateKind.TEAM_TOTAL_OVER,
            ScorePredicateKind.TEAM_TOTAL_UNDER,
        }:
            goals = home_goals if self.arguments[0] == "home" else away_goals
            line = float(self.arguments[1])
            return goals > line if self.kind is ScorePredicateKind.TEAM_TOTAL_OVER else goals < line
        if self.kind is ScorePredicateKind.BTTS_YES:
            return home_goals > 0 and away_goals > 0
        if self.kind is ScorePredicateKind.BTTS_NO:
            return home_goals == 0 or away_goals == 0
        if self.kind is ScorePredicateKind.TOTAL_ODD:
            return (home_goals + away_goals) % 2 == 1
        if self.kind is ScorePredicateKind.TOTAL_EVEN:
            return (home_goals + away_goals) % 2 == 0
        if self.kind is ScorePredicateKind.WINNING_MARGIN:
            margin = home_goals - away_goals
            bucket = self.arguments[0]
            return {
                "draw": margin == 0,
                "home-by-1": margin == 1,
                "home-by-2": margin == 2,
                "home-by-3-plus": margin >= 3,
                "away-by-1": margin == -1,
                "away-by-2": margin == -2,
                "away-by-3-plus": margin <= -3,
            }[bucket]
        if self.kind is ScorePredicateKind.EUROPEAN_HANDICAP:
            side, handicap_text, outcome = self.arguments
            handicap = int(handicap_text)
            adjusted_home = home_goals + (handicap if side == "home" else 0)
            adjusted_away = away_goals + (handicap if side == "away" else 0)
            result = (
                "home"
                if adjusted_home > adjusted_away
                else "draw"
                if adjusted_home == adjusted_away
                else "away"
            )
            return result == outcome
        if self.kind is ScorePredicateKind.AND:
            return all(child.matches(home_goals, away_goals) for child in self.children)
        if self.kind is ScorePredicateKind.OR:
            return any(child.matches(home_goals, away_goals) for child in self.children)
        if self.kind is ScorePredicateKind.NOT:
            return not self.children[0].matches(home_goals, away_goals)
        raise EvaluationError("unsupported score predicate")

    def identity(self) -> str:
        child_identity = ",".join(child.identity() for child in self.children)
        argument_identity = ",".join(self.arguments)
        return f"{self.kind.value}({argument_identity})[{child_identity}]"


@dataclass(frozen=True, slots=True)
class FootballMarketProbability:
    """One coherent model probability and its estimated fair price."""

    market_family: str
    market_key: str
    outcome_key: str
    market_period: str
    participant_scope: str
    line_value: Decimal | None
    probability: float
    fair_decimal_odds: float | None
    predicate: ScorePredicate
    push_probability: float = 0.0
    residual_tail_mass: float = 0.0
    limitation: str | None = None

    def __post_init__(self) -> None:
        if not all((self.market_family, self.market_key, self.outcome_key, self.market_period)):
            raise EvaluationError("market probability identity must be complete")
        for name, value in (
            ("probability", self.probability),
            ("push_probability", self.push_probability),
            ("residual_tail_mass", self.residual_tail_mass),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise EvaluationError(f"{name} must lie in [0, 1]")
        if self.fair_decimal_odds is not None and (
            not math.isfinite(self.fair_decimal_odds) or self.fair_decimal_odds <= 1.0
        ):
            raise EvaluationError("fair decimal odds must be finite and greater than one")

    @property
    def selection_key(self) -> tuple[str, str, str, str, str | None]:
        return (
            self.market_family,
            self.market_key,
            self.outcome_key,
            self.participant_scope,
            None if self.line_value is None else format(self.line_value, "f"),
        )


def primitive(kind: ScorePredicateKind, *arguments: str) -> ScorePredicate:
    return ScorePredicate(kind=kind, arguments=tuple(arguments))


def intersection(*predicates: ScorePredicate) -> ScorePredicate:
    return ScorePredicate(kind=ScorePredicateKind.AND, children=tuple(predicates))


def union(*predicates: ScorePredicate) -> ScorePredicate:
    return ScorePredicate(kind=ScorePredicateKind.OR, children=tuple(predicates))


def complement(predicate: ScorePredicate) -> ScorePredicate:
    return ScorePredicate(kind=ScorePredicateKind.NOT, children=(predicate,))


def predicate_probability(
    distribution: JointScoreDistribution,
    predicate: ScorePredicate,
) -> float:
    """Sum one reviewed predicate over the canonical score matrix."""
    return math.fsum(
        value
        for home_goals, row in enumerate(distribution.probabilities)
        for away_goals, value in enumerate(row)
        if predicate.matches(home_goals, away_goals)
    )


def conjunction_probability(
    distribution: JointScoreDistribution,
    left: ScorePredicate,
    right: ScorePredicate,
) -> float:
    """Exact same-event joint probability; never a marginal product."""
    return predicate_probability(distribution, intersection(left, right))


def conditional_probability(
    distribution: JointScoreDistribution,
    event: ScorePredicate,
    given: ScorePredicate,
) -> float | None:
    denominator = predicate_probability(distribution, given)
    if denominator <= 0.0:
        return None
    return conjunction_probability(distribution, event, given) / denominator


def derive_full_time_markets(
    distribution: JointScoreDistribution,
) -> tuple[FootballMarketProbability, ...]:
    """Derive the reviewed full-time market catalogue from one score surface."""
    rows: list[FootballMarketProbability] = []
    results = {
        "home": primitive(ScorePredicateKind.HOME_WIN),
        "draw": primitive(ScorePredicateKind.DRAW),
        "away": primitive(ScorePredicateKind.AWAY_WIN),
    }
    for outcome, predicate in results.items():
        rows.append(_row(distribution, "match-result", "1x2", outcome, predicate))

    double_chances = {
        "home-or-draw": ("home", "draw"),
        "home-or-away": ("home", "away"),
        "draw-or-away": ("draw", "away"),
    }
    dc_predicates: dict[str, ScorePredicate] = {}
    for outcome, included in double_chances.items():
        predicate = primitive(ScorePredicateKind.DOUBLE_CHANCE, *included)
        dc_predicates[outcome] = predicate
        rows.append(_row(distribution, "double-chance", "three-way-pairs", outcome, predicate))

    draw_probability = predicate_probability(distribution, results["draw"])
    for outcome, win_key in (("home", "home"), ("away", "away")):
        win_probability = predicate_probability(distribution, results[win_key])
        loss_probability = 1.0 - win_probability - draw_probability
        fair = (
            None
            if win_probability <= 0.0
            else (win_probability + loss_probability) / win_probability
        )
        rows.append(
            _row(
                distribution,
                "draw-no-bet",
                "stake-return-on-draw",
                outcome,
                results[win_key],
                probability=(
                    0.0
                    if win_probability + loss_probability <= 0.0
                    else win_probability / (win_probability + loss_probability)
                ),
                fair_odds=fair,
                push_probability=draw_probability,
            )
        )

    for home_goals in range(distribution.score_grid_maximum + 1):
        for away_goals in range(distribution.score_grid_maximum + 1):
            outcome = f"{home_goals}-{away_goals}"
            rows.append(
                _row(
                    distribution,
                    "correct-score",
                    "exact-grid-score",
                    outcome,
                    primitive(
                        ScorePredicateKind.EXACT_SCORE,
                        str(home_goals),
                        str(away_goals),
                    ),
                )
            )

    total_predicates: dict[tuple[str, str], ScorePredicate] = {}
    for line in HALF_GOAL_LINES:
        line_text = format(line, "f")
        for outcome, kind in (
            ("over", ScorePredicateKind.TOTAL_OVER),
            ("under", ScorePredicateKind.TOTAL_UNDER),
        ):
            predicate = primitive(kind, line_text)
            total_predicates[(outcome, line_text)] = predicate
            rows.append(
                _row(
                    distribution,
                    "total-goals",
                    "over-under",
                    outcome,
                    predicate,
                    line=line,
                )
            )

    btts = {
        "yes": primitive(ScorePredicateKind.BTTS_YES),
        "no": primitive(ScorePredicateKind.BTTS_NO),
    }
    for outcome, predicate in btts.items():
        rows.append(_row(distribution, "both-teams-to-score", "yes-no", outcome, predicate))

    for side in ("home", "away"):
        for line in HALF_GOAL_LINES:
            line_text = format(line, "f")
            for outcome, kind in (
                ("over", ScorePredicateKind.TEAM_TOTAL_OVER),
                ("under", ScorePredicateKind.TEAM_TOTAL_UNDER),
            ):
                rows.append(
                    _row(
                        distribution,
                        "team-total-goals",
                        f"{side}-over-under",
                        outcome,
                        primitive(kind, side, line_text),
                        line=line,
                        participant_scope=side,
                    )
                )

    rows.append(
        _row(
            distribution,
            "total-goals-odd-even",
            "parity",
            "odd",
            primitive(ScorePredicateKind.TOTAL_ODD),
        )
    )
    rows.append(
        _row(
            distribution,
            "total-goals-odd-even",
            "parity",
            "even",
            primitive(ScorePredicateKind.TOTAL_EVEN),
        )
    )

    for bucket in (
        "draw",
        "home-by-1",
        "home-by-2",
        "home-by-3-plus",
        "away-by-1",
        "away-by-2",
        "away-by-3-plus",
    ):
        rows.append(
            _row(
                distribution,
                "winning-margin",
                "reviewed-buckets",
                bucket,
                primitive(ScorePredicateKind.WINNING_MARGIN, bucket),
            )
        )

    for side in ("home", "away"):
        for handicap in range(-3, 4):
            line = Decimal(handicap)
            for outcome in ("home", "draw", "away"):
                rows.append(
                    _row(
                        distribution,
                        "european-handicap",
                        f"three-way-{side}-adjusted",
                        outcome,
                        primitive(
                            ScorePredicateKind.EUROPEAN_HANDICAP,
                            side,
                            str(handicap),
                            outcome,
                        ),
                        line=line,
                        participant_scope=side,
                    )
                )

    for result, result_predicate in results.items():
        for line in HALF_GOAL_LINES:
            line_text = format(line, "f")
            for total_outcome in ("over", "under"):
                rows.append(
                    _row(
                        distribution,
                        "result-and-total-goals",
                        "conjunction",
                        f"{result}-and-{total_outcome}",
                        intersection(
                            result_predicate,
                            total_predicates[(total_outcome, line_text)],
                        ),
                        line=line,
                    )
                )
        for btts_outcome, btts_predicate in btts.items():
            rows.append(
                _row(
                    distribution,
                    "result-and-btts",
                    "conjunction",
                    f"{result}-and-{btts_outcome}",
                    intersection(result_predicate, btts_predicate),
                )
            )

    for dc_outcome, dc_predicate in dc_predicates.items():
        for line in HALF_GOAL_LINES:
            line_text = format(line, "f")
            for total_outcome in ("over", "under"):
                rows.append(
                    _row(
                        distribution,
                        "double-chance-and-total-goals",
                        "conjunction",
                        f"{dc_outcome}-and-{total_outcome}",
                        intersection(
                            dc_predicate,
                            total_predicates[(total_outcome, line_text)],
                        ),
                        line=line,
                    )
                )
        for btts_outcome, btts_predicate in btts.items():
            rows.append(
                _row(
                    distribution,
                    "double-chance-and-btts",
                    "conjunction",
                    f"{dc_outcome}-and-{btts_outcome}",
                    intersection(dc_predicate, btts_predicate),
                )
            )

    reviewed_union_line = Decimal("2.5")
    reviewed_union_text = format(reviewed_union_line, "f")
    for result, result_predicate in results.items():
        for total_outcome in ("over", "under"):
            rows.append(
                _row(
                    distribution,
                    "result-or-total-goals",
                    "union",
                    f"{result}-or-{total_outcome}",
                    union(
                        result_predicate,
                        total_predicates[(total_outcome, reviewed_union_text)],
                    ),
                    line=reviewed_union_line,
                )
            )
        for btts_outcome, btts_predicate in btts.items():
            rows.append(
                _row(
                    distribution,
                    "result-or-btts",
                    "union",
                    f"{result}-or-{btts_outcome}",
                    union(result_predicate, btts_predicate),
                )
            )
    for btts_outcome, btts_predicate in btts.items():
        for total_outcome in ("over", "under"):
            rows.append(
                _row(
                    distribution,
                    "btts-or-total-goals",
                    "union",
                    f"{btts_outcome}-or-{total_outcome}",
                    union(
                        btts_predicate,
                        total_predicates[(total_outcome, reviewed_union_text)],
                    ),
                    line=reviewed_union_line,
                )
            )

    return tuple(sorted(rows, key=lambda item: item.selection_key))


def find_market_probability(
    markets: tuple[FootballMarketProbability, ...],
    *,
    market_family: str,
    outcome_key: str,
    line_value: Decimal | None = None,
    participant_scope: str = "event",
) -> FootballMarketProbability:
    """Require one exact canonical market probability."""
    matches = tuple(
        item
        for item in markets
        if item.market_family == market_family
        and item.outcome_key == outcome_key
        and item.line_value == line_value
        and item.participant_scope == participant_scope
    )
    if len(matches) != 1:
        raise EvaluationError("market probability identity is missing or ambiguous")
    return matches[0]


def _row(
    distribution: JointScoreDistribution,
    family: str,
    variant: str,
    outcome: str,
    predicate: ScorePredicate,
    *,
    line: Decimal | None = None,
    participant_scope: str = "event",
    probability: float | None = None,
    fair_odds: float | None = None,
    push_probability: float = 0.0,
) -> FootballMarketProbability:
    model_probability = (
        predicate_probability(distribution, predicate) if probability is None else probability
    )
    fair = fair_odds if fair_odds is not None else _fair_odds(model_probability)
    limitation = (
        "zero-probability-unpriced"
        if fair is None
        else ("residual-tail-within-tolerance" if distribution.residual_tail_mass > 0.0 else None)
    )
    return FootballMarketProbability(
        market_family=family,
        market_key=build_market_key(
            sport_code="football",
            market_family=family,
            variant=variant,
            market_period=FULL_MATCH,
        ),
        outcome_key=outcome,
        market_period=FULL_MATCH,
        participant_scope=participant_scope,
        line_value=line,
        probability=model_probability,
        fair_decimal_odds=fair,
        predicate=predicate,
        push_probability=push_probability,
        residual_tail_mass=distribution.residual_tail_mass,
        limitation=limitation,
    )


def _fair_odds(probability: float) -> float | None:
    if probability <= 0.0:
        return None
    return 1.0 / probability


def _validate_predicate_arguments(
    kind: ScorePredicateKind,
    arguments: tuple[str, ...],
) -> None:
    no_arguments = {
        ScorePredicateKind.HOME_WIN,
        ScorePredicateKind.DRAW,
        ScorePredicateKind.AWAY_WIN,
        ScorePredicateKind.BTTS_YES,
        ScorePredicateKind.BTTS_NO,
        ScorePredicateKind.TOTAL_ODD,
        ScorePredicateKind.TOTAL_EVEN,
        ScorePredicateKind.AND,
        ScorePredicateKind.OR,
        ScorePredicateKind.NOT,
    }
    if kind in no_arguments and arguments:
        raise EvaluationError(f"{kind.value} does not accept arguments")
    if kind is ScorePredicateKind.DOUBLE_CHANCE:
        if (
            len(arguments) != 2
            or len(set(arguments)) != 2
            or not set(arguments)
            <= {
                "home",
                "draw",
                "away",
            }
        ):
            raise EvaluationError("double-chance arguments are invalid")
    if kind is ScorePredicateKind.EXACT_SCORE:
        if len(arguments) != 2 or any(not value.isdigit() for value in arguments):
            raise EvaluationError("exact-score arguments must be non-negative integers")
    if kind in {ScorePredicateKind.TOTAL_OVER, ScorePredicateKind.TOTAL_UNDER}:
        if len(arguments) != 1:
            raise EvaluationError("total predicates require one line")
        _positive_half_line(arguments[0])
    if kind in {
        ScorePredicateKind.TEAM_TOTAL_OVER,
        ScorePredicateKind.TEAM_TOTAL_UNDER,
    }:
        if len(arguments) != 2 or arguments[0] not in {"home", "away"}:
            raise EvaluationError("team-total predicates require side and line")
        _positive_half_line(arguments[1])
    if kind is ScorePredicateKind.WINNING_MARGIN and (
        len(arguments) != 1
        or arguments[0]
        not in {
            "draw",
            "home-by-1",
            "home-by-2",
            "home-by-3-plus",
            "away-by-1",
            "away-by-2",
            "away-by-3-plus",
        }
    ):
        raise EvaluationError("winning-margin bucket is invalid")
    if kind is ScorePredicateKind.EUROPEAN_HANDICAP:
        if (
            len(arguments) != 3
            or arguments[0] not in {"home", "away"}
            or arguments[2] not in {"home", "draw", "away"}
        ):
            raise EvaluationError("European handicap arguments are invalid")
        try:
            handicap = int(arguments[1])
        except ValueError as exc:
            raise EvaluationError("European handicap line must be an integer") from exc
        if str(handicap) != arguments[1] or not -10 <= handicap <= 10:
            raise EvaluationError("European handicap line is outside reviewed bounds")


def _positive_half_line(value: str) -> None:
    try:
        line = Decimal(value)
    except Exception as exc:
        raise EvaluationError("score predicate line is malformed") from exc
    if line < 0 or line % 1 != Decimal("0.5"):
        raise EvaluationError("score predicate line must be a non-negative half-goal line")

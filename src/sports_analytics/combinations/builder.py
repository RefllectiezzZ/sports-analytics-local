"""Bounded deterministic combination construction."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from sports_analytics.combinations.contracts import (
    Combination,
    CombinationRules,
    DependencyClass,
    classify_dependency,
    validate_combination,
)
from sports_analytics.core.exceptions import CombinationError
from sports_analytics.opportunities.contracts import Opportunity, opportunity_rank_key


@dataclass(frozen=True, slots=True)
class BuilderBounds:
    """Hard limits preventing combinatorial work from becoming unbounded."""

    maximum_candidates: int = 50
    maximum_evaluated_combinations: int = 10_000
    maximum_results: int = 100

    def __post_init__(self) -> None:
        for field_name in (
            "maximum_candidates",
            "maximum_evaluated_combinations",
            "maximum_results",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise CombinationError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class CombinationRejection:
    """Auditable rejected leg set."""

    opportunity_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class CombinationBuildResult:
    """Ranked combinations and bounded validation audit."""

    combinations: tuple[Combination, ...]
    rejections: tuple[CombinationRejection, ...]
    candidates_considered: int
    combinations_evaluated: int
    truncated: bool


def build_combinations(
    opportunities: tuple[Opportunity, ...],
    *,
    rules: CombinationRules,
    bounds: BuilderBounds | None = None,
) -> CombinationBuildResult:
    """Enumerate stable candidate tuples within explicit hard bounds."""
    limits = bounds or BuilderBounds()
    by_id: dict[str, Opportunity] = {}
    for opportunity in opportunities:
        if opportunity.opportunity_id in by_id:
            raise CombinationError(f"duplicate opportunity id: {opportunity.opportunity_id}")
        by_id[opportunity.opportunity_id] = opportunity
    candidate_limit = min(limits.maximum_candidates, rules.maximum_candidates)
    evaluated_limit = min(
        limits.maximum_evaluated_combinations,
        rules.maximum_evaluated_combinations,
    )
    result_limit = min(limits.maximum_results, rules.maximum_outputs)
    candidates = sorted(by_id.values(), key=opportunity_rank_key)[:candidate_limit]
    built: list[Combination] = []
    rejected: list[CombinationRejection] = []
    evaluated = 0
    truncated = len(by_id) > len(candidates)
    stop = False
    for leg_count in range(rules.minimum_legs, rules.maximum_legs + 1):
        for leg_tuple in combinations(candidates, leg_count):
            if evaluated >= evaluated_limit:
                truncated = True
                stop = True
                break
            evaluated += 1
            ids = tuple(sorted(item.opportunity_id for item in leg_tuple))
            early_reason = _early_rejection_reason(leg_tuple, rules=rules)
            if early_reason is not None:
                rejected.append(CombinationRejection(opportunity_ids=ids, reason=early_reason))
                continue
            try:
                built.append(validate_combination(tuple(leg_tuple), rules=rules, automatic=True))
            except CombinationError as exc:
                rejected.append(CombinationRejection(opportunity_ids=ids, reason=str(exc)))
        if stop:
            break
    built.sort(key=_combination_rank_key)
    if len(built) > result_limit:
        truncated = True
        built = built[:result_limit]
    return CombinationBuildResult(
        combinations=tuple(built),
        rejections=tuple(rejected),
        candidates_considered=len(candidates),
        combinations_evaluated=evaluated,
        truncated=truncated,
    )


def _combination_rank_key(item: Combination) -> tuple[float, str]:
    return (-item.expected_value, item.combination_id)


def _early_rejection_reason(
    legs: tuple[Opportunity, ...],
    *,
    rules: CombinationRules,
) -> str | None:
    """Reject unsafe or impossible candidates before full identity construction."""
    odds = Decimal("1")
    for leg in legs:
        if leg.evaluation_mode.value != "live-safe":
            return "production combinations refuse closing-line historical benchmark quotes"
        if not rules.selection_minimum_odds <= leg.decimal_odds <= rules.selection_maximum_odds:
            return "a leg is outside selection_odds_range"
        odds *= leg.decimal_odds
        if odds > rules.combined_maximum_odds:
            return "combination is outside combined_odds_range"
    starts = [leg.event_start_utc for leg in legs]
    if max(starts) - min(starts) > rules.maximum_event_horizon:
        return "combination exceeds maximum_event_horizon"
    for index, left in enumerate(legs):
        for right in legs[index + 1 :]:
            relation = classify_dependency(left, right)
            if relation.classification is DependencyClass.CONFLICT:
                return f"conflicting legs: {relation.reason}"
            if relation.classification is DependencyClass.UNKNOWN:
                return f"unknown dependency rejected: {relation.reason}"
    return None

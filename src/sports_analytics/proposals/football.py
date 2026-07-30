"""Deterministic football singles and same-bookmaker accumulator proposals."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from pathlib import Path
from typing import Final, cast

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.bookmakers.operator_quotes import (
    OperatorQuoteCatalogue,
    ValidatedOperatorQuote,
)
from sports_analytics.core.exceptions import ArtifactError, CombinationError, EvaluationError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.markets.football_score_markets import (
    FootballMarketProbability,
    JointScoreDistribution,
    conjunction_probability,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.proposals.abstention import AbstentionReason, canonical_abstention_codes

PROPOSED_BETS_ARTIFACT_TYPE: Final[str] = "football-proposed-bets"
PROPOSED_BETS_ARTIFACT_SCHEMA: Final[str] = "football-proposed-bets-v1"
_CANONICAL_SPORTS: Final[frozenset[str]] = frozenset({"football", "basketball", "tennis"})


class SportCombinationMode(StrEnum):
    COMBINE_SELECTED_SPORTS = "combine-selected-sports"
    SEPARATE_BY_SPORT = "separate-by-sport"


@dataclass(frozen=True, slots=True)
class ProposalSportPolicy:
    """Persisted domain policy applied before candidate enumeration."""

    allowed_sports: tuple[str, ...] = ("football",)
    mode: SportCombinationMode = SportCombinationMode.COMBINE_SELECTED_SPORTS

    def __post_init__(self) -> None:
        if (
            not self.allowed_sports
            or tuple(sorted(set(self.allowed_sports))) != self.allowed_sports
            or not set(self.allowed_sports) <= _CANONICAL_SPORTS
        ):
            raise EvaluationError(
                "allowed_sports must be non-empty, canonical, unique, and ordered"
            )

    @property
    def policy_id(self) -> str:
        return content_addressed_id(
            identity_type="proposal-sport-policy-v1",
            payload={
                "allowed_sports": cast(list[JsonValue], list(self.allowed_sports)),
                "mode": self.mode.value,
            },
        )


@dataclass(frozen=True, slots=True)
class FootballOpportunityPolicy:
    """Conservative price-based acceptance policy."""

    minimum_offered_odds: Decimal = Decimal("1.20")
    maximum_offered_odds: Decimal = Decimal("20.00")
    minimum_edge: float = 0.02
    minimum_expected_value: float = 0.03
    safety_margin: float = 0.005
    minimum_total_odds: float = 1.0
    maximum_total_odds: float = 1_000_000.0
    maximum_uncertainty: float = 1.0
    maximum_residual_tail_mass: float = 1e-6
    reject_fallback_predictions: bool = True
    maximum_candidates: int = 50
    maximum_accumulators: int = 100
    minimum_legs: int = 2
    maximum_legs: int = 4
    sport_policy: ProposalSportPolicy = ProposalSportPolicy()

    def __post_init__(self) -> None:
        if (
            not self.minimum_offered_odds.is_finite()
            or not self.maximum_offered_odds.is_finite()
            or self.minimum_offered_odds <= 1
            or self.maximum_offered_odds < self.minimum_offered_odds
        ):
            raise EvaluationError("proposal offered-odds bounds are invalid")
        for field_name in (
            "minimum_edge",
            "minimum_expected_value",
            "safety_margin",
            "maximum_residual_tail_mass",
            "maximum_uncertainty",
        ):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value < 0.0:
                raise EvaluationError(f"{field_name} must be finite and non-negative")
        if (
            not math.isfinite(self.minimum_total_odds)
            or not math.isfinite(self.maximum_total_odds)
            or self.minimum_total_odds < 1.0
            or self.maximum_total_odds < self.minimum_total_odds
        ):
            raise EvaluationError("proposal total-odds bounds are invalid")
        for field_name in (
            "maximum_candidates",
            "maximum_accumulators",
            "minimum_legs",
            "maximum_legs",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise EvaluationError(f"{field_name} must be a positive integer")
        if self.minimum_legs > self.maximum_legs:
            raise EvaluationError("minimum_legs cannot exceed maximum_legs")


@dataclass(frozen=True, slots=True)
class ProposedSingleDecision:
    """Accepted or abstained current-price decision."""

    decision_id: str
    sport_code: str
    canonical_event_id: str
    model_artifact_id: str
    model_probability: float
    fair_decimal_odds: float | None
    offered_decimal_odds: Decimal | None
    market_probability: float | None
    edge: float | None
    expected_value: float | None
    provider_id: str | None
    quote_observation_id: str | None
    decision_as_of_utc: datetime
    accepted: bool
    reason_codes: tuple[str, ...]
    market: FootballMarketProbability
    uncertainty_state: str
    placement_state: str = "manual-only"


@dataclass(frozen=True, slots=True)
class ProposedAccumulator:
    """One same-provider, separate-event informational accumulator."""

    proposal_id: str
    provider_id: str
    sport_codes: tuple[str, ...]
    legs: tuple[ProposedSingleDecision, ...]
    total_offered_odds: Decimal
    joint_probability: float
    expected_value: float
    common_decision_time_utc: datetime
    dependency_state: str
    placement_state: str = "manual-only"


@dataclass(frozen=True, slots=True)
class AnalyticalSameEventConjunction:
    """Exact analytical conjunction, placeable only with a real combined quote."""

    canonical_event_id: str
    left_selection_key: tuple[str, str, str, str, str | None]
    right_selection_key: tuple[str, str, str, str, str | None]
    conjunction_probability: float
    offered_combined_odds: Decimal | None
    expected_value: float | None
    provider_id: str | None
    placeable: bool
    limitation: str | None


@dataclass(frozen=True, slots=True)
class ProposalRun:
    """Bounded accepted/rejected singles and accumulator read model."""

    decisions: tuple[ProposedSingleDecision, ...]
    accumulators: tuple[ProposedAccumulator, ...]
    accumulator_rejections: tuple[str, ...]
    sport_policy: ProposalSportPolicy
    sport_statuses: tuple[tuple[str, str], ...]


def evaluate_proposed_single(
    *,
    canonical_event_id: str,
    market: FootballMarketProbability,
    quote: ValidatedOperatorQuote | None,
    model_artifact_id: str,
    decision_as_of_utc: datetime,
    policy: FootballOpportunityPolicy | None = None,
    fallback_used: bool = False,
    calibration_acceptable: bool = True,
    production_champion_available: bool = True,
    real_historical_evidence_sufficient: bool = True,
    competition_evidence_sufficient: bool = True,
    champion_stale: bool = False,
    retraining_overdue: bool = False,
    result_evidence_complete: bool = True,
    bootstrap_state: str = "available",
    bootstrap_interval_too_wide: bool = False,
    candidate_disagreement_too_high: bool = False,
    rho_stable: bool = True,
    player_context_state: str = "not-requested",
) -> ProposedSingleDecision:
    """Evaluate one selection while preserving fair/offered terminology."""
    rules = policy or FootballOpportunityPolicy()
    reasons: list[str] = []
    offered: Decimal | None = None
    market_probability: float | None = None
    edge: float | None = None
    expected_value: float | None = None
    provider_id: str | None = None
    quote_observation_id: str | None = None
    if not production_champion_available:
        reasons.append(AbstentionReason.NO_PRODUCTION_CHAMPION.value)
    if not real_historical_evidence_sufficient:
        reasons.append(AbstentionReason.INSUFFICIENT_REAL_HISTORICAL_EVIDENCE.value)
    if not competition_evidence_sufficient:
        reasons.append(AbstentionReason.INSUFFICIENT_COMPETITION_EVIDENCE.value)
    if champion_stale:
        reasons.append(AbstentionReason.STALE_CHAMPION.value)
    if retraining_overdue:
        reasons.append(AbstentionReason.RETRAINING_OVERDUE.value)
    if not result_evidence_complete:
        reasons.append(AbstentionReason.RESULT_EVIDENCE_INCOMPLETE.value)
    if bootstrap_state != "available":
        reasons.append(AbstentionReason.BOOTSTRAP_UNCERTAINTY_UNAVAILABLE.value)
    if bootstrap_interval_too_wide:
        reasons.append(AbstentionReason.BOOTSTRAP_INTERVAL_TOO_WIDE.value)
    if candidate_disagreement_too_high:
        reasons.append(AbstentionReason.CANDIDATE_DISAGREEMENT_TOO_HIGH.value)
    if not rho_stable:
        reasons.append(AbstentionReason.RHO_STABILITY_WARNING.value)
    if player_context_state == "stale":
        reasons.append(AbstentionReason.PLAYER_CONTEXT_STALE.value)
    elif player_context_state == "unresolved":
        reasons.append(AbstentionReason.PLAYER_CONTEXT_UNRESOLVED.value)
    elif player_context_state == "train-serve-equivalence-unavailable":
        reasons.append(AbstentionReason.PLAYER_TRAIN_SERVE_EQUIVALENCE_UNAVAILABLE.value)
    if market.fair_decimal_odds is None or market.probability <= 0.0:
        reasons.append(AbstentionReason.SCORE_PROBABILITY_UNPRICED.value)
    if market.residual_tail_mass > rules.maximum_residual_tail_mass:
        reasons.append(AbstentionReason.TAIL_MASS_DEGRADED.value)
    if fallback_used and rules.reject_fallback_predictions:
        reasons.append(AbstentionReason.HISTORY_FALLBACK.value)
    if not calibration_acceptable:
        reasons.append(AbstentionReason.MODEL_CALIBRATION_FAILED.value)
    if quote is None:
        reasons.append(AbstentionReason.QUOTE_UNAVAILABLE.value)
    else:
        provider_id = quote.input.provider_id
        quote_observation_id = quote.odds_quote.quote_observation_id
        offered = quote.offered_decimal_odds
        if quote.input.canonical_event_id != canonical_event_id:
            reasons.append(AbstentionReason.EVENT_UNRESOLVED.value)
        if not _market_matches_quote(market, quote):
            reasons.append(AbstentionReason.MARKET_UNSUPPORTED.value)
        if not quote.market_complete:
            reasons.append(AbstentionReason.QUOTE_INCOMPLETE.value)
        else:
            market_probability = quote.market_probability
            if market_probability is None:
                reasons.append(AbstentionReason.DEPENDENCY_UNKNOWN.value)
            else:
                edge = market.probability - market_probability
        expected_value = market.probability * float(offered) - 1.0
        if offered < rules.minimum_offered_odds:
            reasons.append(AbstentionReason.OFFERED_ODDS_BELOW_MINIMUM.value)
        if offered > rules.maximum_offered_odds:
            reasons.append(AbstentionReason.OFFERED_ODDS_ABOVE_MAXIMUM.value)
        if edge is not None and edge < rules.minimum_edge + rules.safety_margin:
            reasons.append(AbstentionReason.EDGE_INSUFFICIENT.value)
        if expected_value < rules.minimum_expected_value:
            reasons.append(AbstentionReason.CONSERVATIVE_EV_INSUFFICIENT.value)
    canonical_reasons = canonical_abstention_codes(tuple(reasons))
    accepted = not canonical_reasons
    identity_payload: dict[str, JsonValue] = {
        "sport_code": "football",
        "canonical_event_id": canonical_event_id,
        "model_artifact_id": model_artifact_id,
        "market_key": market.market_key,
        "outcome_key": market.outcome_key,
        "line_value": None if market.line_value is None else format(market.line_value, "f"),
        "model_probability": market.probability,
        "fair_decimal_odds": market.fair_decimal_odds,
        "offered_decimal_odds": None if offered is None else format(offered, "f"),
        "market_probability": market_probability,
        "edge": edge,
        "expected_value": expected_value,
        "provider_id": provider_id,
        "quote_observation_id": quote_observation_id,
        "decision_as_of_utc": format_utc_timestamp(decision_as_of_utc),
        "accepted": accepted,
        "reason_codes": cast(list[JsonValue], list(canonical_reasons)),
    }
    decision_id = content_addressed_id(
        identity_type="football-proposed-single-decision-v1",
        payload=identity_payload,
    )
    return ProposedSingleDecision(
        decision_id=decision_id,
        sport_code="football",
        canonical_event_id=canonical_event_id,
        model_artifact_id=model_artifact_id,
        model_probability=market.probability,
        fair_decimal_odds=market.fair_decimal_odds,
        offered_decimal_odds=offered,
        market_probability=market_probability,
        edge=edge,
        expected_value=expected_value,
        provider_id=provider_id,
        quote_observation_id=quote_observation_id,
        decision_as_of_utc=decision_as_of_utc,
        accepted=accepted,
        reason_codes=canonical_reasons,
        market=market,
        uncertainty_state=(
            "fallback"
            if fallback_used
            else ("tail-within-tolerance" if market.residual_tail_mass > 0.0 else "reviewed")
        ),
    )


def evaluate_catalogue_proposals(
    *,
    event_markets: dict[str, tuple[FootballMarketProbability, ...]],
    catalogue: OperatorQuoteCatalogue | None,
    model_artifact_ids: dict[str, str],
    decision_as_of_utc: datetime,
    policy: FootballOpportunityPolicy | None = None,
) -> ProposalRun:
    """Evaluate fair-odds-only or current-price product paths deterministically."""
    rules = policy or FootballOpportunityPolicy()
    quote_index: dict[tuple[object, ...], ValidatedOperatorQuote] = {}
    if catalogue is not None:
        for quote in catalogue.quotes:
            key = _quote_match_key(quote)
            if key in quote_index:
                raise EvaluationError("validated operator catalogue has ambiguous quote identity")
            quote_index[key] = quote
    decisions: list[ProposedSingleDecision] = []
    if "football" in rules.sport_policy.allowed_sports:
        for event_id in sorted(event_markets):
            model_id = model_artifact_ids.get(event_id)
            if not model_id:
                raise EvaluationError("every event requires score-model artifact lineage")
            for market in event_markets[event_id]:
                selected_quote = quote_index.get(_market_match_key(event_id, market))
                decisions.append(
                    evaluate_proposed_single(
                        canonical_event_id=event_id,
                        market=market,
                        quote=selected_quote,
                        model_artifact_id=model_id,
                        decision_as_of_utc=decision_as_of_utc,
                        policy=rules,
                    )
                )
    accepted = tuple(item for item in decisions if item.accepted)
    accumulators, rejections, statuses = build_same_bookmaker_accumulators(
        accepted,
        policy=rules,
    )
    return ProposalRun(
        decisions=tuple(sorted(decisions, key=lambda item: item.decision_id)),
        accumulators=accumulators,
        accumulator_rejections=rejections,
        sport_policy=rules.sport_policy,
        sport_statuses=statuses,
    )


def build_same_bookmaker_accumulators(
    decisions: tuple[ProposedSingleDecision, ...],
    *,
    policy: FootballOpportunityPolicy | None = None,
) -> tuple[
    tuple[ProposedAccumulator, ...],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
]:
    """Build bounded separate-event accumulators using only real offered legs."""
    rules = policy or FootballOpportunityPolicy()
    selected = tuple(
        item for item in decisions if item.sport_code in rules.sport_policy.allowed_sports
    )
    eligible = tuple(
        sorted(
            (item for item in selected if item.accepted),
            key=lambda item: (-float(item.expected_value or 0.0), item.decision_id),
        )[: rules.maximum_candidates]
    )
    proposals: list[ProposedAccumulator] = []
    rejections: list[str] = []
    statuses = tuple(
        (
            sport,
            (
                "sport-model-unavailable"
                if sport != "football"
                else (
                    "operational"
                    if any(item.accepted and item.sport_code == sport for item in selected)
                    else "no-eligible-opportunity"
                )
            ),
        )
        for sport in rules.sport_policy.allowed_sports
    )
    groups = (
        (eligible,)
        if rules.sport_policy.mode is SportCombinationMode.COMBINE_SELECTED_SPORTS
        else tuple(
            tuple(item for item in eligible if item.sport_code == sport)
            for sport in rules.sport_policy.allowed_sports
        )
    )
    for group in groups:
        _build_accumulator_group(group, rules=rules, proposals=proposals, rejections=rejections)
        if len(proposals) >= rules.maximum_accumulators:
            break
    proposals.sort(key=lambda item: (-item.expected_value, item.proposal_id))
    return tuple(proposals), tuple(sorted(set(rejections))), statuses


def _build_accumulator_group(
    eligible: tuple[ProposedSingleDecision, ...],
    *,
    rules: FootballOpportunityPolicy,
    proposals: list[ProposedAccumulator],
    rejections: list[str],
) -> None:
    for leg_count in range(rules.minimum_legs, rules.maximum_legs + 1):
        for legs in combinations(eligible, leg_count):
            if len(proposals) >= rules.maximum_accumulators:
                rejections.append("accumulator-search-truncated")
                break
            providers = {item.provider_id for item in legs}
            if len(providers) != 1 or None in providers:
                rejections.append("mixed-provider-accumulator-rejected")
                continue
            event_ids = {item.canonical_event_id for item in legs}
            if len(event_ids) != len(legs):
                rejections.append("same-event-marginal-product-forbidden")
                continue
            if any(
                item.offered_decimal_odds is None or item.quote_observation_id is None
                for item in legs
            ):
                rejections.append("accumulator-leg-missing-offered-price")
                continue
            total_odds = Decimal("1")
            for leg in legs:
                assert leg.offered_decimal_odds is not None
                total_odds *= leg.offered_decimal_odds
            if not rules.minimum_total_odds <= float(total_odds) <= rules.maximum_total_odds:
                rejections.append("accumulator-total-odds-outside-policy")
                continue
            joint_probability = math.prod(item.model_probability for item in legs)
            expected_value = joint_probability * float(total_odds) - 1.0
            common_time = max(item.decision_as_of_utc for item in legs)
            provider = next(iter(providers))
            assert provider is not None
            identity: dict[str, JsonValue] = {
                "provider_id": provider,
                "sport_codes": cast(
                    list[JsonValue],
                    sorted({item.sport_code for item in legs}),
                ),
                "decision_ids": cast(
                    list[JsonValue],
                    sorted(item.decision_id for item in legs),
                ),
                "quote_observation_ids": cast(
                    list[JsonValue],
                    sorted(str(item.quote_observation_id) for item in legs),
                ),
                "total_offered_odds": format(total_odds, "f"),
                "joint_probability": joint_probability,
                "expected_value": expected_value,
                "common_decision_time_utc": format_utc_timestamp(common_time),
            }
            proposals.append(
                ProposedAccumulator(
                    proposal_id=content_addressed_id(
                        identity_type="football-same-bookmaker-accumulator-v1",
                        payload=identity,
                    ),
                    provider_id=provider,
                    sport_codes=tuple(sorted({item.sport_code for item in legs})),
                    legs=tuple(sorted(legs, key=lambda item: item.decision_id)),
                    total_offered_odds=total_odds,
                    joint_probability=joint_probability,
                    expected_value=expected_value,
                    common_decision_time_utc=common_time,
                    dependency_state="structurally-separate-events",
                )
            )
        if len(proposals) >= rules.maximum_accumulators:
            break


def analyse_same_event_conjunction(
    *,
    canonical_event_id: str,
    distribution: JointScoreDistribution,
    left: FootballMarketProbability,
    right: FootballMarketProbability,
    offered_combined_quote: ValidatedOperatorQuote | None = None,
) -> AnalyticalSameEventConjunction:
    """Calculate exact dependence and refuse synthetic same-game offered prices."""
    probability = conjunction_probability(
        distribution,
        left.predicate,
        right.predicate,
    )
    if offered_combined_quote is None:
        return AnalyticalSameEventConjunction(
            canonical_event_id=canonical_event_id,
            left_selection_key=left.selection_key,
            right_selection_key=right.selection_key,
            conjunction_probability=probability,
            offered_combined_odds=None,
            expected_value=None,
            provider_id=None,
            placeable=False,
            limitation="analytical-conjunction-only: real combined offered price required",
        )
    if offered_combined_quote.input.canonical_event_id != canonical_event_id:
        raise CombinationError("combined offered quote belongs to a different event")
    offered = offered_combined_quote.offered_decimal_odds
    return AnalyticalSameEventConjunction(
        canonical_event_id=canonical_event_id,
        left_selection_key=left.selection_key,
        right_selection_key=right.selection_key,
        conjunction_probability=probability,
        offered_combined_odds=offered,
        expected_value=probability * float(offered) - 1.0,
        provider_id=offered_combined_quote.input.provider_id,
        placeable=True,
        limitation=None,
    )


def proposal_run_payload(run: ProposalRun) -> dict[str, JsonValue]:
    """Build the persisted Streamlit read model."""
    return {
        "price_semantics": {
            "fair_odds": "model-estimate",
            "offered_odds": "real-external-price",
            "ev_requires_offered_odds": True,
        },
        "placement_state": "manual-only",
        "sport_policy": {
            "allowed_sports": list(run.sport_policy.allowed_sports),
            "mode": run.sport_policy.mode.value,
            "policy_id": run.sport_policy.policy_id,
        },
        "sport_statuses": [
            {"sport_code": sport, "status": status} for sport, status in run.sport_statuses
        ],
        "singles": [_single_payload(item) for item in run.decisions],
        "accumulators": [
            {
                "proposal_id": item.proposal_id,
                "provider_id": item.provider_id,
                "sport_codes": list(item.sport_codes),
                "decision_ids": [leg.decision_id for leg in item.legs],
                "event_ids": [leg.canonical_event_id for leg in item.legs],
                "offered_odds_per_leg": [
                    format(leg.offered_decimal_odds, "f")
                    for leg in item.legs
                    if leg.offered_decimal_odds is not None
                ],
                "total_offered_odds": format(item.total_offered_odds, "f"),
                "joint_probability": item.joint_probability,
                "expected_value": item.expected_value,
                "common_decision_time_utc": format_utc_timestamp(item.common_decision_time_utc),
                "dependency_state": item.dependency_state,
                "same_bookmaker_confirmed": True,
                "placement_state": item.placement_state,
            }
            for item in run.accumulators
        ],
        "accumulator_rejections": list(run.accumulator_rejections),
    }


def write_proposal_artifact(
    *,
    root: Path,
    relative_directory: str,
    run: ProposalRun,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=PROPOSED_BETS_ARTIFACT_TYPE,
        schema_version=PROPOSED_BETS_ARTIFACT_SCHEMA,
        payload=proposal_run_payload(run),
    )


def load_proposal_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    """Strictly verify fair/offered separation and manual-only proposal formulas."""
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=PROPOSED_BETS_ARTIFACT_TYPE,
        expected_schema_version=PROPOSED_BETS_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "price_semantics",
        "placement_state",
        "sport_policy",
        "sport_statuses",
        "singles",
        "accumulators",
        "accumulator_rejections",
    }:
        raise ArtifactError("football proposal artifact fields are not exact")
    if payload["placement_state"] != "manual-only":
        raise ArtifactError("football proposal artifact claims non-manual placement")
    semantics = payload["price_semantics"]
    if not isinstance(semantics, dict) or semantics != {
        "fair_odds": "model-estimate",
        "offered_odds": "real-external-price",
        "ev_requires_offered_odds": True,
    }:
        raise ArtifactError("football proposal price semantics are invalid")
    singles = payload["singles"]
    accumulators = payload["accumulators"]
    if not isinstance(singles, list) or not isinstance(accumulators, list):
        raise ArtifactError("football proposal rows are malformed")
    sport_policy = payload["sport_policy"]
    if not isinstance(sport_policy, dict):
        raise ArtifactError("proposal sport policy is malformed")
    try:
        reloaded_policy = ProposalSportPolicy(
            allowed_sports=tuple(cast(list[str], sport_policy["allowed_sports"])),
            mode=SportCombinationMode(str(sport_policy["mode"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactError("proposal sport policy is malformed") from exc
    if sport_policy.get("policy_id") != reloaded_policy.policy_id:
        raise ArtifactError("proposal sport policy identity mismatch")
    for row in singles:
        if not isinstance(row, dict) or row.get("placement_state") != "manual-only":
            raise ArtifactError("football single placement state is invalid")
        offered = row.get("offered_decimal_odds")
        expected = row.get("expected_value")
        if offered is None and (expected is not None or row.get("accepted") is not False):
            raise ArtifactError("single without offered odds cannot have EV or be accepted")
        if offered is not None:
            try:
                odds = float(Decimal(str(offered)))
                probability_raw = row["model_probability"]
                if isinstance(probability_raw, bool) or not isinstance(
                    probability_raw, int | float
                ):
                    raise TypeError
                if isinstance(expected, bool) or not isinstance(expected, int | float):
                    raise TypeError
                probability = float(probability_raw)
                expected_number = float(expected)
            except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                raise ArtifactError("football single price formula is malformed") from exc
            if not math.isclose(
                expected_number,
                probability * odds - 1.0,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ArtifactError("football single expected value was tampered")
    for row in accumulators:
        if (
            not isinstance(row, dict)
            or row.get("placement_state") != "manual-only"
            or row.get("same_bookmaker_confirmed") is not True
        ):
            raise ArtifactError("football accumulator trust state is invalid")
        event_ids = row.get("event_ids")
        if not isinstance(event_ids, list) or len(event_ids) != len(set(event_ids)):
            raise ArtifactError("football accumulator must contain separate events")
    return artifact


def _single_payload(item: ProposedSingleDecision) -> dict[str, JsonValue]:
    return {
        "decision_id": item.decision_id,
        "sport_code": item.sport_code,
        "canonical_event_id": item.canonical_event_id,
        "provider_id": item.provider_id,
        "quote_observation_id": item.quote_observation_id,
        "model_artifact_id": item.model_artifact_id,
        "market_family": item.market.market_family,
        "market_key": item.market.market_key,
        "outcome_key": item.market.outcome_key,
        "line_value": (
            None if item.market.line_value is None else format(item.market.line_value, "f")
        ),
        "model_probability": item.model_probability,
        "fair_decimal_odds": item.fair_decimal_odds,
        "offered_decimal_odds": (
            None if item.offered_decimal_odds is None else format(item.offered_decimal_odds, "f")
        ),
        "market_probability": item.market_probability,
        "edge": item.edge,
        "expected_value": item.expected_value,
        "uncertainty_state": item.uncertainty_state,
        "decision_as_of_utc": format_utc_timestamp(item.decision_as_of_utc),
        "accepted": item.accepted,
        "reason_codes": list(item.reason_codes),
        "placement_state": item.placement_state,
    }


def _market_matches_quote(
    market: FootballMarketProbability,
    quote: ValidatedOperatorQuote,
) -> bool:
    item = quote.input
    return (
        item.sport_code == "football"
        and item.market_family == market.market_family
        and item.outcome_key == market.outcome_key
        and item.line_value == market.line_value
        and item.market_period == market.market_period
        and item.participant_scope == market.participant_scope
    )


def _market_match_key(
    event_id: str,
    market: FootballMarketProbability,
) -> tuple[object, ...]:
    return (
        event_id,
        market.market_family,
        market.outcome_key,
        market.line_value,
        market.market_period,
        market.participant_scope,
    )


def _quote_match_key(quote: ValidatedOperatorQuote) -> tuple[object, ...]:
    item = quote.input
    return (
        item.canonical_event_id,
        item.market_family,
        item.outcome_key,
        item.line_value,
        item.market_period,
        item.participant_scope,
    )

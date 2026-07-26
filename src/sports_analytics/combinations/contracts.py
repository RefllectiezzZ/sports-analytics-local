"""Generic dependency and timing contracts for multi-event combinations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast

from sports_analytics.core.exceptions import CombinationError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.contracts import Opportunity
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.value.contracts import QuoteEvaluationMode


class DependencyClass(StrEnum):
    """Deterministic v1 relation between two proposed legs."""

    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    STRUCTURALLY_SEPARATE = "structurally_separate"


@dataclass(frozen=True, slots=True)
class LegDependency:
    """Auditable pairwise dependency classification."""

    left_opportunity_id: str
    right_opportunity_id: str
    classification: DependencyClass
    reason: str


@dataclass(frozen=True, slots=True)
class CombinationRules:
    """Independent per-leg and total-price bounds."""

    minimum_legs: int = 2
    maximum_legs: int = 4
    selection_minimum_odds: Decimal = Decimal("1.0001")
    selection_maximum_odds: Decimal = Decimal("100000")
    combined_minimum_odds: Decimal = Decimal("1.0001")
    combined_maximum_odds: Decimal = Decimal("1000000")
    allow_unknown_dependencies: bool = False
    policy_version: str = "combination-policy-v1"
    allowed_sport_codes: frozenset[str] = frozenset()
    allowed_market_keys: frozenset[str] = frozenset()
    minimum_joint_probability: float = 0.0
    minimum_expected_value: float = -1.0
    maximum_candidates: int = 50
    maximum_evaluated_combinations: int = 10_000
    maximum_outputs: int = 100
    maximum_event_horizon: timedelta = timedelta(days=365)
    allow_multiple_sports: bool = True
    allow_multiple_dates: bool = True

    def __post_init__(self) -> None:
        if type(self.minimum_legs) is not int or type(self.maximum_legs) is not int:
            raise CombinationError("combination leg bounds must be integers")
        if self.minimum_legs < 2 or self.maximum_legs < self.minimum_legs:
            raise CombinationError("combination leg bounds must satisfy 2 <= minimum <= maximum")
        for lower, upper, name in (
            (
                self.selection_minimum_odds,
                self.selection_maximum_odds,
                "selection_odds_range",
            ),
            (
                self.combined_minimum_odds,
                self.combined_maximum_odds,
                "combined_odds_range",
            ),
        ):
            if not lower.is_finite() or not upper.is_finite() or lower <= 1 or upper < lower:
                raise CombinationError(f"{name} must be finite, >1, and ordered")
        if type(self.policy_version) is not str or not self.policy_version:
            raise CombinationError("policy_version must be a non-empty string")
        for field_name in (
            "maximum_candidates",
            "maximum_evaluated_combinations",
            "maximum_outputs",
        ):
            if type(getattr(self, field_name)) is not int or getattr(self, field_name) < 1:
                raise CombinationError(f"{field_name} must be a positive integer")
        for field_name in ("minimum_joint_probability", "minimum_expected_value"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise CombinationError(f"{field_name} must be numeric")
            if not math.isfinite(float(value)):
                raise CombinationError(f"{field_name} must be finite")
        if not 0.0 <= self.minimum_joint_probability <= 1.0:
            raise CombinationError("minimum_joint_probability must lie in [0, 1]")
        if self.minimum_expected_value < -1.0:
            raise CombinationError("minimum_expected_value must be at least -1")
        if not isinstance(self.maximum_event_horizon, timedelta):
            raise CombinationError("maximum_event_horizon must be a timedelta")
        if self.maximum_event_horizon < timedelta(0):
            raise CombinationError("maximum_event_horizon must not be negative")
        for field_name in (
            "allow_unknown_dependencies",
            "allow_multiple_sports",
            "allow_multiple_dates",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise CombinationError(f"{field_name} must be boolean")
        for field_name in ("allowed_sport_codes", "allowed_market_keys"):
            if any(type(item) is not str or not item for item in getattr(self, field_name)):
                raise CombinationError(f"{field_name} must contain non-empty strings")

    @property
    def policy_id(self) -> str:
        payload: dict[str, JsonValue] = {
            "policy_version": self.policy_version,
            "minimum_legs": self.minimum_legs,
            "maximum_legs": self.maximum_legs,
            "selection_minimum_odds": format(self.selection_minimum_odds, "f"),
            "selection_maximum_odds": format(self.selection_maximum_odds, "f"),
            "combined_minimum_odds": format(self.combined_minimum_odds, "f"),
            "combined_maximum_odds": format(self.combined_maximum_odds, "f"),
            "allow_unknown_dependencies": self.allow_unknown_dependencies,
            "allowed_sport_codes": cast(
                list[JsonValue],
                sorted(self.allowed_sport_codes),
            ),
            "allowed_market_keys": cast(
                list[JsonValue],
                sorted(self.allowed_market_keys),
            ),
            "minimum_joint_probability": self.minimum_joint_probability,
            "minimum_expected_value": self.minimum_expected_value,
            "maximum_candidates": self.maximum_candidates,
            "maximum_evaluated_combinations": self.maximum_evaluated_combinations,
            "maximum_outputs": self.maximum_outputs,
            "maximum_event_horizon_microseconds": (
                self.maximum_event_horizon.days * 86_400_000_000
                + self.maximum_event_horizon.seconds * 1_000_000
                + self.maximum_event_horizon.microseconds
            ),
            "allow_multiple_sports": self.allow_multiple_sports,
            "allow_multiple_dates": self.allow_multiple_dates,
        }
        return content_addressed_id(identity_type=self.policy_version, payload=payload)


@dataclass(frozen=True, slots=True)
class CombinationValidationResult:
    """Auditable manual validation outcome that may be ineligible."""

    legs: tuple[Opportunity, ...]
    dependencies: tuple[LegDependency, ...]
    combination: Combination | None
    eligible: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Combination:
    """A validated multi-sport/multi-market/multi-date combination."""

    combination_id: str
    legs: tuple[Opportunity, ...]
    combined_decimal_odds: Decimal
    earliest_event_start_utc: datetime
    common_information_time_utc: datetime
    dependencies: tuple[LegDependency, ...]
    leg_count: int
    total_decimal_odds: Decimal
    joint_probability: float
    expected_value: float
    latest_event_start_utc: datetime
    policy_version: str
    policy_id: str
    eligible: bool
    rejection_reasons: tuple[str, ...]
    structural_independence_warning: str


def derive_combination_id(
    *,
    opportunity_ids: list[str],
    combined_decimal_odds: str,
    joint_probability: float,
    expected_value: float,
    common_information_time_utc: str,
    policy_id: str,
) -> str:
    """Derive one canonical combination identity from material leg and policy content."""
    return content_addressed_id(
        identity_type="combination-v1",
        payload={
            "opportunity_ids": cast(list[JsonValue], opportunity_ids),
            "combined_decimal_odds": combined_decimal_odds,
            "joint_probability": joint_probability,
            "expected_value": expected_value,
            "common_information_time_utc": common_information_time_utc,
            "policy_id": policy_id,
        },
    )


def classify_dependency(left: Opportunity, right: Opportunity) -> LegDependency:
    """Classify a pair conservatively without estimating correlation."""
    return _classify_dependency_pair(
        left_opportunity_id=left.opportunity_id,
        right_opportunity_id=right.opportunity_id,
        left_canonical_event_id=left.canonical_event_id,
        right_canonical_event_id=right.canonical_event_id,
        left_selection=left.selection,
        right_selection=right.selection,
        left_dependency_keys=left.dependency_keys,
        right_dependency_keys=right.dependency_keys,
        left_participant_ids=left.participant_ids,
        right_participant_ids=right.participant_ids,
        left_dependency_metadata_complete=left.dependency_metadata_complete,
        right_dependency_metadata_complete=right.dependency_metadata_complete,
    )


def classify_dependency_from_opportunity_rows(
    left_row: Mapping[str, object],
    right_row: Mapping[str, object],
) -> LegDependency:
    """Classify one opportunity pair from persisted artifact rows without trusting labels."""
    from sports_analytics.artifact_strict import (
        require_bool,
        require_canonical_selection_identity,
        require_str,
    )

    left_keys = _dependency_keys_from_row(left_row, field_prefix="left")
    right_keys = _dependency_keys_from_row(right_row, field_prefix="right")
    return _classify_dependency_pair(
        left_opportunity_id=require_str(left_row.get("opportunity_id"), field="opportunity_id"),
        right_opportunity_id=require_str(right_row.get("opportunity_id"), field="opportunity_id"),
        left_canonical_event_id=require_str(
            left_row.get("canonical_event_id"),
            field="canonical_event_id",
        ),
        right_canonical_event_id=require_str(
            right_row.get("canonical_event_id"),
            field="canonical_event_id",
        ),
        left_selection=require_canonical_selection_identity(
            left_row.get("selection"),
            field="selection",
        ),
        right_selection=require_canonical_selection_identity(
            right_row.get("selection"),
            field="selection",
        ),
        left_dependency_keys=left_keys,
        right_dependency_keys=right_keys,
        left_participant_ids=_participant_ids_from_row(left_row, field_prefix="left"),
        right_participant_ids=_participant_ids_from_row(right_row, field_prefix="right"),
        left_dependency_metadata_complete=require_bool(
            left_row.get("dependency_metadata_complete"),
            field="dependency_metadata_complete",
        ),
        right_dependency_metadata_complete=require_bool(
            right_row.get("dependency_metadata_complete"),
            field="dependency_metadata_complete",
        ),
    )


def _dependency_keys_from_row(row: Mapping[str, object], *, field_prefix: str) -> frozenset[str]:
    from sports_analytics.artifact_strict import require_list, require_str

    keys = require_list(row.get("dependency_keys"), field=f"{field_prefix}.dependency_keys")
    return frozenset(require_str(item, field=f"{field_prefix}.dependency_keys[]") for item in keys)


def _participant_ids_from_row(row: Mapping[str, object], *, field_prefix: str) -> frozenset[str]:
    from sports_analytics.artifact_strict import require_list, require_str

    participants = require_list(row.get("participant_ids"), field=f"{field_prefix}.participant_ids")
    return frozenset(
        require_str(item, field=f"{field_prefix}.participant_ids[]") for item in participants
    )


def _classify_dependency_pair(
    *,
    left_opportunity_id: str,
    right_opportunity_id: str,
    left_canonical_event_id: str,
    right_canonical_event_id: str,
    left_selection: CanonicalSelectionIdentity,
    right_selection: CanonicalSelectionIdentity,
    left_dependency_keys: frozenset[str],
    right_dependency_keys: frozenset[str],
    left_participant_ids: frozenset[str],
    right_participant_ids: frozenset[str],
    left_dependency_metadata_complete: bool,
    right_dependency_metadata_complete: bool,
) -> LegDependency:
    """Shared deterministic dependency classification for typed and row-based inputs."""
    ordered = sorted((left_opportunity_id, right_opportunity_id))
    if left_opportunity_id == right_opportunity_id or (
        left_canonical_event_id == right_canonical_event_id and left_selection == right_selection
    ):
        classification = DependencyClass.CONFLICT
        reason = "duplicate canonical selection"
    elif left_canonical_event_id == right_canonical_event_id:
        if left_selection.market_identity == right_selection.market_identity:
            classification = DependencyClass.CONFLICT
            reason = "different outcomes of the same canonical event market"
        else:
            classification = DependencyClass.UNKNOWN
            reason = "same event across different markets has unmodelled dependency"
    elif not left_dependency_metadata_complete or not right_dependency_metadata_complete:
        classification = DependencyClass.UNKNOWN
        reason = "dependency or participant metadata is incomplete"
    elif _contradictory_dependency_keys(left_dependency_keys, right_dependency_keys):
        classification = DependencyClass.CONFLICT
        reason = "dependency keys contain contradictory exclusive claims"
    elif left_dependency_keys & right_dependency_keys:
        classification = DependencyClass.UNKNOWN
        reason = "distinct events share dependency keys"
    elif left_participant_ids & right_participant_ids:
        classification = DependencyClass.UNKNOWN
        reason = "distinct events share canonical participants"
    else:
        classification = DependencyClass.STRUCTURALLY_SEPARATE
        reason = "complete metadata proves distinct dependency keys and participants"
    return LegDependency(
        left_opportunity_id=ordered[0],
        right_opportunity_id=ordered[1],
        classification=classification,
        reason=reason,
    )


def validate_combination_manual(
    legs: tuple[Opportunity, ...],
    *,
    rules: CombinationRules,
) -> CombinationValidationResult:
    """Validate exact legs and return an auditable result without trusting unknown dependencies."""
    ordered = tuple(sorted(legs, key=lambda item: item.opportunity_id))
    dependencies: list[LegDependency] = []
    rejection_reasons: list[str] = []
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            relation = classify_dependency(left, right)
            dependencies.append(relation)
            if relation.classification is DependencyClass.CONFLICT:
                rejection_reasons.append(f"conflicting legs: {relation.reason}")
            elif relation.classification is DependencyClass.UNKNOWN:
                rejection_reasons.append(f"unknown dependency rejected: {relation.reason}")
    if rejection_reasons:
        return CombinationValidationResult(
            legs=ordered,
            dependencies=tuple(dependencies),
            combination=None,
            eligible=False,
            rejection_reasons=tuple(rejection_reasons),
        )
    try:
        combination = validate_combination(ordered, rules=rules, automatic=False)
    except CombinationError as exc:
        return CombinationValidationResult(
            legs=ordered,
            dependencies=tuple(dependencies),
            combination=None,
            eligible=False,
            rejection_reasons=(str(exc),),
        )
    return CombinationValidationResult(
        legs=ordered,
        dependencies=tuple(dependencies),
        combination=combination,
        eligible=True,
        rejection_reasons=(),
    )


def validate_combination(
    legs: tuple[Opportunity, ...],
    *,
    rules: CombinationRules,
    automatic: bool = False,
) -> Combination:
    """Manually validate exact legs using the same rules as the automatic builder."""
    if not rules.minimum_legs <= len(legs) <= rules.maximum_legs:
        raise CombinationError("combination leg count is outside configured bounds")
    ordered = tuple(sorted(legs, key=lambda item: item.opportunity_id))
    if len({item.opportunity_id for item in ordered}) != len(ordered):
        raise CombinationError("combination contains duplicate opportunity ids")
    for leg in ordered:
        if leg.evaluation_mode is not QuoteEvaluationMode.LIVE_SAFE:
            raise CombinationError(
                "production combinations refuse closing-line historical benchmark quotes"
            )
        if not rules.selection_minimum_odds <= leg.decimal_odds <= rules.selection_maximum_odds:
            raise CombinationError("a leg is outside selection_odds_range")
        if leg.quoted_at_utc is None:
            raise CombinationError("every combination leg requires a provider quote timestamp")
        if rules.allowed_sport_codes and leg.selection.sport_code not in rules.allowed_sport_codes:
            raise CombinationError("a leg sport is outside allowed_sport_codes")
        if rules.allowed_market_keys and leg.selection.market_key not in rules.allowed_market_keys:
            raise CombinationError("a leg market is outside allowed_market_keys")
    dependencies: list[LegDependency] = []
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            relation = classify_dependency(left, right)
            dependencies.append(relation)
            if relation.classification is DependencyClass.CONFLICT:
                raise CombinationError(f"conflicting legs: {relation.reason}")
            if relation.classification is DependencyClass.UNKNOWN:
                raise CombinationError(f"unknown dependency rejected: {relation.reason}")
    earliest_start = min(item.event_start_utc for item in ordered)
    latest_start = max(item.event_start_utc for item in ordered)
    information_time = max(cast(datetime, item.decision_as_of_utc) for item in ordered)
    if information_time >= earliest_start:
        raise CombinationError(
            "max(decision information times) must be strictly before earliest event start"
        )
    combined_odds = Decimal("1")
    for leg in ordered:
        combined_odds *= leg.decimal_odds
    if not rules.combined_minimum_odds <= combined_odds <= rules.combined_maximum_odds:
        raise CombinationError("combination is outside combined_odds_range")
    sport_codes = {item.selection.sport_code for item in ordered}
    event_dates = {item.event_start_utc.date() for item in ordered}
    if not rules.allow_multiple_sports and len(sport_codes) > 1:
        raise CombinationError("multiple sports are disabled by combination policy")
    if not rules.allow_multiple_dates and len(event_dates) > 1:
        raise CombinationError("multiple event dates are disabled by combination policy")
    if latest_start - earliest_start > rules.maximum_event_horizon:
        raise CombinationError("combination exceeds maximum_event_horizon")
    joint_probability = math.prod(item.model_probability for item in ordered)
    expected_value = joint_probability * float(combined_odds) - 1.0
    if joint_probability < rules.minimum_joint_probability:
        raise CombinationError("combination is below minimum_joint_probability")
    if expected_value < rules.minimum_expected_value:
        raise CombinationError("combination is below minimum_expected_value")
    identity = derive_combination_id(
        opportunity_ids=[item.opportunity_id for item in ordered],
        combined_decimal_odds=format(combined_odds, "f"),
        joint_probability=joint_probability,
        expected_value=expected_value,
        common_information_time_utc=format_utc_timestamp(information_time),
        policy_id=rules.policy_id,
    )
    return Combination(
        combination_id=identity,
        legs=ordered,
        combined_decimal_odds=combined_odds,
        earliest_event_start_utc=earliest_start,
        common_information_time_utc=information_time,
        dependencies=tuple(dependencies),
        leg_count=len(ordered),
        total_decimal_odds=combined_odds,
        joint_probability=joint_probability,
        expected_value=expected_value,
        latest_event_start_utc=latest_start,
        policy_version=rules.policy_version,
        policy_id=rules.policy_id,
        eligible=True,
        rejection_reasons=(),
        structural_independence_warning=(
            "Joint probability is a product under a structural-independence "
            "approximation; no correlation estimate is implied."
        ),
    )


def _contradictory_dependency_keys(
    left: frozenset[str],
    right: frozenset[str],
) -> bool:
    """Detect explicit ``exclusive:<scope>=<value>`` contradictions."""

    def claims(keys: frozenset[str]) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for key in keys:
            if not key.startswith("exclusive:") or "=" not in key:
                continue
            scope, value = key.removeprefix("exclusive:").split("=", maxsplit=1)
            if scope and value:
                parsed[scope] = value
        return parsed

    left_claims = claims(left)
    right_claims = claims(right)
    return any(
        scope in right_claims and right_claims[scope] != value
        for scope, value in left_claims.items()
    )

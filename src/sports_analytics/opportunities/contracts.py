"""Typed opportunity filtering, audit rejection, and deterministic ranking."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from sports_analytics.core.exceptions import OpportunityError, RepositoryError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.markets.contracts import validate_decimal_odds
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.sports.contracts import require_utc
from sports_analytics.value.contracts import (
    VALUE_EVALUATION_VERSION,
    MarketValueEvaluation,
    QuoteEvaluationMode,
)


class RejectionCode(StrEnum):
    """Stable machine-readable reasons an evaluated selection was rejected."""

    HISTORICAL_BENCHMARK = "historical-benchmark"
    SPORT = "sport"
    MARKET = "market"
    PROVIDER = "provider"
    EVENT_START = "event-start"
    PROBABILITY = "probability"
    EDGE = "edge"
    EXPECTED_VALUE = "expected-value"
    SELECTION_ODDS = "selection-odds"
    RANK_CAP = "rank-cap"
    PREDICTION_QUALITY = "prediction-quality"


class OpportunityRankingMode(StrEnum):
    """Versioned deterministic accepted-opportunity ordering."""

    EXPECTED_VALUE = "expected-value-v1"
    MODEL_PROBABILITY = "model-probability-v1"
    EDGE = "edge-v1"


@dataclass(frozen=True, slots=True)
class Opportunity:
    """One fully evaluated canonical selection."""

    opportunity_id: str
    canonical_event_id: str
    event_start_utc: datetime
    selection: CanonicalSelectionIdentity
    prediction_id: str
    predicted_at_utc: datetime
    model_trained_through_date: date
    model_calibrated_through_date: date
    quote_observation_id: str
    quote_series_id: str
    quoted_at_utc: datetime | None
    source_observed_at_utc: datetime
    source_name: str
    provider_type: str
    provider_id: str
    evaluation_mode: QuoteEvaluationMode
    decimal_odds: Decimal
    model_probability: float
    raw_implied_probability: float
    normalized_implied_probability: float
    overround: float
    edge: float
    expected_value: float
    decision_as_of_utc: datetime | None = None
    model_artifact_id: str = ""
    model_checksum_sha256: str = ""
    model_specification_version: str = ""
    feature_artifact_id: str = ""
    feature_manifest_checksum_sha256: str = ""
    feature_specification_version: str = ""
    feature_row_id: str = ""
    dependency_keys: frozenset[str] = frozenset()
    participant_ids: frozenset[str] = frozenset()
    dependency_metadata_complete: bool = False
    prediction_quality_passed: bool = False
    evaluation_version: str = VALUE_EVALUATION_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "opportunity_id",
            "canonical_event_id",
            "prediction_id",
            "quote_observation_id",
            "quote_series_id",
            "source_name",
            "provider_type",
            "provider_id",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise OpportunityError(f"{field_name} must be a non-empty string")
        try:
            object.__setattr__(
                self,
                "event_start_utc",
                require_utc(self.event_start_utc, field_name="event_start_utc"),
            )
            object.__setattr__(
                self,
                "predicted_at_utc",
                require_utc(self.predicted_at_utc, field_name="predicted_at_utc"),
            )
            object.__setattr__(
                self,
                "source_observed_at_utc",
                require_utc(
                    self.source_observed_at_utc,
                    field_name="source_observed_at_utc",
                ),
            )
            if self.quoted_at_utc is not None:
                object.__setattr__(
                    self,
                    "quoted_at_utc",
                    require_utc(self.quoted_at_utc, field_name="quoted_at_utc"),
                )
            object.__setattr__(
                self,
                "decimal_odds",
                validate_decimal_odds(self.decimal_odds),
            )
            object.__setattr__(
                self,
                "evaluation_mode",
                QuoteEvaluationMode(self.evaluation_mode),
            )
        except Exception as exc:
            raise OpportunityError(str(exc)) from exc
        exact_quote_time = self.quoted_at_utc
        if self.evaluation_mode is QuoteEvaluationMode.LIVE_SAFE:
            if exact_quote_time is None:
                raise OpportunityError("live-safe opportunity requires exact quote time")
            expected_decision_time = max(
                self.predicted_at_utc,
                exact_quote_time,
                self.source_observed_at_utc,
            )
        else:
            expected_decision_time = self.event_start_utc
        if self.decision_as_of_utc is not None:
            try:
                supplied = require_utc(
                    self.decision_as_of_utc,
                    field_name="decision_as_of_utc",
                )
            except Exception as exc:
                raise OpportunityError(str(exc)) from exc
            if supplied != expected_decision_time:
                raise OpportunityError("decision_as_of_utc must equal the derived decision time")
        object.__setattr__(self, "decision_as_of_utc", expected_decision_time)
        from sports_analytics.opportunities.identity import verify_opportunity_identity

        verify_opportunity_identity(self)
        if (
            type(self.model_trained_through_date) is not date
            or type(self.model_calibrated_through_date) is not date
        ):
            raise OpportunityError("model lineage cutoffs must be dates")
        for field_name in (
            "model_probability",
            "raw_implied_probability",
            "normalized_implied_probability",
            "overround",
            "edge",
            "expected_value",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise OpportunityError(f"{field_name} must be numeric")
            if not math.isfinite(float(value)):
                raise OpportunityError(f"{field_name} must be finite")
        lineage_fields = (
            self.model_artifact_id,
            self.model_checksum_sha256,
            self.model_specification_version,
            self.feature_artifact_id,
            self.feature_manifest_checksum_sha256,
            self.feature_specification_version,
            self.feature_row_id,
        )
        if not all(lineage_fields):
            raise OpportunityError("opportunity lineage fields must be complete")
        try:
            validate_sha256_checksum(self.model_checksum_sha256)
            validate_sha256_checksum(self.feature_manifest_checksum_sha256)
        except RepositoryError as exc:
            raise OpportunityError("opportunity lineage checksum is malformed") from exc
        if self.feature_row_id != self.canonical_event_id:
            raise OpportunityError("feature_row_id must match canonical_event_id")
        if type(self.dependency_metadata_complete) is not bool:
            raise OpportunityError("dependency_metadata_complete must be boolean")
        if type(self.prediction_quality_passed) is not bool:
            raise OpportunityError("prediction_quality_passed must be boolean")
        if any(type(item) is not str or not item for item in self.dependency_keys):
            raise OpportunityError("dependency keys must be non-empty strings")
        if any(type(item) is not str or not item for item in self.participant_ids):
            raise OpportunityError("participant ids must be non-empty strings")
        if self.dependency_metadata_complete and (
            not self.dependency_keys or not self.participant_ids
        ):
            raise OpportunityError("complete dependency metadata requires keys and participant ids")


@dataclass(frozen=True, slots=True)
class OpportunityFilter:
    """Fixed filters applied independently to every selection."""

    minimum_probability: float = 0.0
    minimum_edge: float = 0.0
    minimum_expected_value: float = 0.0
    selection_minimum_odds: Decimal = Decimal("1.0001")
    selection_maximum_odds: Decimal = Decimal("100000")
    sport_codes: frozenset[str] = frozenset()
    market_keys: frozenset[str] = frozenset()
    provider_ids: frozenset[str] = frozenset()
    starts_at_or_after_utc: datetime | None = None
    starts_before_utc: datetime | None = None
    include_historical_benchmarks: bool = False
    filter_version: str = "opportunity-filter-v1"
    ranking_mode: OpportunityRankingMode = OpportunityRankingMode.EXPECTED_VALUE
    max_accepted_count: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "minimum_probability",
            "minimum_edge",
            "minimum_expected_value",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise OpportunityError(f"{field_name} must be numeric")
            if not math.isfinite(float(value)):
                raise OpportunityError(f"{field_name} must be finite")
        if not 0.0 <= self.minimum_probability <= 1.0:
            raise OpportunityError("minimum_probability must lie in [0, 1]")
        if type(self.filter_version) is not str or not self.filter_version:
            raise OpportunityError("filter_version must be a non-empty string")
        try:
            object.__setattr__(self, "ranking_mode", OpportunityRankingMode(self.ranking_mode))
        except ValueError as exc:
            raise OpportunityError("unsupported opportunity ranking mode") from exc
        if self.max_accepted_count is not None and (
            type(self.max_accepted_count) is not int or self.max_accepted_count < 1
        ):
            raise OpportunityError("max_accepted_count must be a positive integer")
        if type(self.include_historical_benchmarks) is not bool:
            raise OpportunityError("include_historical_benchmarks must be boolean")
        for field_name in ("sport_codes", "market_keys", "provider_ids"):
            values = getattr(self, field_name)
            if any(type(item) is not str or not item for item in values):
                raise OpportunityError(f"{field_name} must contain non-empty strings")
        if (
            not self.selection_minimum_odds.is_finite()
            or not self.selection_maximum_odds.is_finite()
            or self.selection_minimum_odds <= 1
            or self.selection_maximum_odds < self.selection_minimum_odds
        ):
            raise OpportunityError("selection odds range must be finite, >1, and ordered")
        try:
            if self.starts_at_or_after_utc is not None:
                object.__setattr__(
                    self,
                    "starts_at_or_after_utc",
                    require_utc(
                        self.starts_at_or_after_utc,
                        field_name="starts_at_or_after_utc",
                    ),
                )
            if self.starts_before_utc is not None:
                object.__setattr__(
                    self,
                    "starts_before_utc",
                    require_utc(self.starts_before_utc, field_name="starts_before_utc"),
                )
        except Exception as exc:
            raise OpportunityError(str(exc)) from exc
        if (
            self.starts_at_or_after_utc is not None
            and self.starts_before_utc is not None
            and self.starts_before_utc <= self.starts_at_or_after_utc
        ):
            raise OpportunityError("event start filter range must be increasing")

    @property
    def filter_config_id(self) -> str:
        payload: dict[str, JsonValue] = {
            "filter_version": self.filter_version,
            "minimum_probability": self.minimum_probability,
            "minimum_edge": self.minimum_edge,
            "minimum_expected_value": self.minimum_expected_value,
            "selection_minimum_odds": format(self.selection_minimum_odds, "f"),
            "selection_maximum_odds": format(self.selection_maximum_odds, "f"),
            "sport_codes": cast(list[JsonValue], sorted(self.sport_codes)),
            "market_keys": cast(list[JsonValue], sorted(self.market_keys)),
            "provider_ids": cast(list[JsonValue], sorted(self.provider_ids)),
            "starts_at_or_after_utc": (
                None
                if self.starts_at_or_after_utc is None
                else format_utc_timestamp(self.starts_at_or_after_utc)
            ),
            "starts_before_utc": (
                None
                if self.starts_before_utc is None
                else format_utc_timestamp(self.starts_before_utc)
            ),
            "include_historical_benchmarks": self.include_historical_benchmarks,
            "ranking_mode": self.ranking_mode.value,
            "max_accepted_count": self.max_accepted_count,
        }
        return content_addressed_id(identity_type=self.filter_version, payload=payload)


@dataclass(frozen=True, slots=True)
class OpportunityRejection:
    """Auditable reasons associated with one rejected opportunity."""

    opportunity: Opportunity
    codes: tuple[RejectionCode, ...]


@dataclass(frozen=True, slots=True)
class OpportunitySearchResult:
    """Deterministically ranked accepted opportunities plus all rejections."""

    accepted: tuple[Opportunity, ...]
    rejected: tuple[OpportunityRejection, ...]
    decisions: tuple[OpportunityDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class OpportunityDecision:
    """Persistable eligibility decision under one exact filter configuration."""

    opportunity_id: str
    filter_config_id: str
    decision_as_of_utc: datetime
    eligible: bool
    rejection_codes: tuple[RejectionCode, ...]
    accepted_rank: int | None


def opportunities_from_evaluation(
    evaluation: MarketValueEvaluation,
) -> tuple[Opportunity, ...]:
    """Project every complete-market value row into a verified immutable opportunity."""
    from sports_analytics.opportunities.identity import build_opportunity_from_evaluation

    priced = {item.selection.selection_id: item for item in evaluation.quote.selections}
    opportunities: list[Opportunity] = []
    for value in evaluation.selections:
        quote_selection = priced[value.selection.selection_id]
        opportunities.append(
            build_opportunity_from_evaluation(
                evaluation,
                value,
                quote_observation_id=quote_selection.quote_observation_id,
                quote_series_id=quote_selection.quote_series_id,
            )
        )
    return tuple(opportunities)


def filter_and_rank_opportunities(
    opportunities: tuple[Opportunity, ...],
    *,
    filters: OpportunityFilter,
) -> OpportunitySearchResult:
    """Apply all filters with audit reasons, then rank by stable value keys."""
    seen: set[str] = set()
    accepted: list[Opportunity] = []
    rejected: list[OpportunityRejection] = []
    for opportunity in sorted(opportunities, key=lambda item: item.opportunity_id):
        if opportunity.opportunity_id in seen:
            raise OpportunityError(f"duplicate opportunity id: {opportunity.opportunity_id}")
        seen.add(opportunity.opportunity_id)
        codes = _rejection_codes(opportunity, filters)
        if codes:
            rejected.append(OpportunityRejection(opportunity=opportunity, codes=codes))
        else:
            accepted.append(opportunity)
    accepted.sort(key=lambda item: opportunity_rank_key(item, mode=filters.ranking_mode))
    if filters.max_accepted_count is not None:
        overflow = accepted[filters.max_accepted_count :]
        accepted = accepted[: filters.max_accepted_count]
        rejected.extend(
            OpportunityRejection(opportunity=item, codes=(RejectionCode.RANK_CAP,))
            for item in overflow
        )
    rejection_by_id = {
        rejection.opportunity.opportunity_id: rejection.codes for rejection in rejected
    }
    accepted_rank = {
        opportunity.opportunity_id: rank for rank, opportunity in enumerate(accepted, start=1)
    }
    decisions = tuple(
        OpportunityDecision(
            opportunity_id=opportunity.opportunity_id,
            filter_config_id=filters.filter_config_id,
            decision_as_of_utc=cast(datetime, opportunity.decision_as_of_utc),
            eligible=opportunity.opportunity_id in accepted_rank,
            rejection_codes=rejection_by_id.get(opportunity.opportunity_id, ()),
            accepted_rank=accepted_rank.get(opportunity.opportunity_id),
        )
        for opportunity in sorted(opportunities, key=lambda item: item.opportunity_id)
    )
    return OpportunitySearchResult(
        accepted=tuple(accepted),
        rejected=tuple(sorted(rejected, key=lambda item: item.opportunity.opportunity_id)),
        decisions=decisions,
    )


def opportunity_rank_key(
    item: Opportunity,
    *,
    mode: OpportunityRankingMode = OpportunityRankingMode.EXPECTED_VALUE,
) -> tuple[float, float, str, str]:
    """Sort by a versioned score then canonical event and selection identities."""
    if mode is OpportunityRankingMode.EXPECTED_VALUE:
        primary = item.expected_value
        secondary = item.model_probability
    elif mode is OpportunityRankingMode.MODEL_PROBABILITY:
        primary = item.model_probability
        secondary = item.expected_value
    else:
        primary = item.edge
        secondary = item.expected_value
    return (
        -primary,
        -secondary,
        item.canonical_event_id,
        item.selection.selection_id,
    )


def _rejection_codes(
    opportunity: Opportunity,
    filters: OpportunityFilter,
) -> tuple[RejectionCode, ...]:
    codes: list[RejectionCode] = []
    if (
        opportunity.evaluation_mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK
        and not filters.include_historical_benchmarks
    ):
        codes.append(RejectionCode.HISTORICAL_BENCHMARK)
    if filters.sport_codes and opportunity.selection.sport_code not in filters.sport_codes:
        codes.append(RejectionCode.SPORT)
    if filters.market_keys and opportunity.selection.market_key not in filters.market_keys:
        codes.append(RejectionCode.MARKET)
    if filters.provider_ids and opportunity.provider_id not in filters.provider_ids:
        codes.append(RejectionCode.PROVIDER)
    if (
        filters.starts_at_or_after_utc is not None
        and opportunity.event_start_utc < filters.starts_at_or_after_utc
    ):
        codes.append(RejectionCode.EVENT_START)
    if (
        filters.starts_before_utc is not None
        and opportunity.event_start_utc >= filters.starts_before_utc
    ):
        codes.append(RejectionCode.EVENT_START)
    if opportunity.model_probability < filters.minimum_probability:
        codes.append(RejectionCode.PROBABILITY)
    if opportunity.edge < filters.minimum_edge:
        codes.append(RejectionCode.EDGE)
    if opportunity.expected_value < filters.minimum_expected_value:
        codes.append(RejectionCode.EXPECTED_VALUE)
    if not (
        filters.selection_minimum_odds <= opportunity.decimal_odds <= filters.selection_maximum_odds
    ):
        codes.append(RejectionCode.SELECTION_ODDS)
    if not opportunity.prediction_quality_passed:
        if not (
            opportunity.evaluation_mode is QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK
            and filters.include_historical_benchmarks
        ):
            codes.append(RejectionCode.PREDICTION_QUALITY)
    return tuple(codes)

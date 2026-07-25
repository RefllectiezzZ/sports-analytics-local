"""Canonical opportunity identity and verified construction."""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sports_analytics.core.exceptions import OpportunityError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc
from sports_analytics.value.contracts import (
    MarketValueEvaluation,
    QuoteEvaluationMode,
    SelectionValue,
)
from sports_analytics.value.timing import compute_decision_as_of

if TYPE_CHECKING:
    from sports_analytics.opportunities.contracts import Opportunity

VALUE_CALCULATION_TOLERANCE: float = 1e-9


def derive_opportunity_id(
    *,
    evaluation_version: str,
    mode: QuoteEvaluationMode,
    prediction_id: str,
    quote_observation_id: str,
    selection_id: str,
    source_name: str,
    provider_type: str,
    provider_id: str,
    decimal_odds: Decimal,
    decision_as_of_utc: datetime,
) -> str:
    """Derive one content-addressed opportunity identity from material inputs."""
    return content_addressed_id(
        identity_type="opportunity-v1",
        payload={
            "evaluation_version": evaluation_version,
            "mode": mode.value,
            "prediction_id": prediction_id,
            "quote_observation_id": quote_observation_id,
            "selection_id": selection_id,
            "source_name": source_name,
            "provider_type": provider_type,
            "provider_id": provider_id,
            "decimal_odds": format(decimal_odds, "f"),
            "decision_as_of_utc": format_utc_timestamp(
                require_utc(decision_as_of_utc, field_name="decision_as_of_utc")
            ),
        },
    )


def derive_evaluation_id(
    *,
    prediction_id: str,
    quote_observation_id: str,
    selection_id: str,
) -> str:
    """Derive one market-evaluation row identity."""
    return content_addressed_id(
        identity_type="market-evaluation-v1",
        payload={
            "prediction_id": prediction_id,
            "quote_observation_id": quote_observation_id,
            "selection_id": selection_id,
        },
    )


def verify_selection_value_calculations(
    *,
    model_probability: float,
    decimal_odds: Decimal,
    raw_implied_probability: float,
    normalized_implied_probability: float,
    overround: float,
    edge: float,
    expected_value: float,
    complete_market_raw_total: float,
) -> None:
    """Recompute and verify one selection's value calculations."""
    if not 0.0 <= model_probability <= 1.0:
        raise OpportunityError("model probability must lie in [0, 1]")
    odds = float(decimal_odds)
    if not math.isfinite(odds) or odds <= 1.0:
        raise OpportunityError("decimal odds must be finite and >1")
    expected_raw = 1.0 / odds
    if not math.isfinite(expected_raw) or expected_raw <= 0.0:
        raise OpportunityError("raw implied probability must be positive and finite")
    if abs(raw_implied_probability - expected_raw) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("raw implied probability does not match decimal odds")
    if not 0.0 <= normalized_implied_probability <= 1.0:
        raise OpportunityError("normalized implied probability must lie in [0, 1]")
    expected_normalized = expected_raw / complete_market_raw_total
    if abs(normalized_implied_probability - expected_normalized) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("normalized implied probability is inconsistent with overround")
    expected_edge = model_probability - expected_normalized
    if abs(edge - expected_edge) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("probability edge is inconsistent with model and market")
    expected_ev = model_probability * odds - 1.0
    if abs(expected_value - expected_ev) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("expected value is inconsistent with model probability and odds")
    for field_name, value in (
        ("overround", overround),
        ("edge", edge),
        ("expected_value", expected_value),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
        ):
            raise OpportunityError(f"{field_name} must be finite")


def build_opportunity_from_evaluation(
    evaluation: MarketValueEvaluation,
    value: SelectionValue,
    *,
    quote_observation_id: str,
    dependency_keys: frozenset[str] | None = None,
    participant_ids: frozenset[str] | None = None,
    dependency_metadata_complete: bool = False,
) -> Opportunity:
    """Construct one verified opportunity from a complete market evaluation."""
    from sports_analytics.opportunities.contracts import Opportunity

    prediction = evaluation.prediction
    quote = evaluation.quote
    decision_as_of = compute_decision_as_of(
        prediction=prediction,
        quote=quote,
        mode=evaluation.mode,
    )
    complete_market_raw_total = math.fsum(
        1.0 / float(item.decimal_odds) for item in evaluation.quote.selections
    )
    verify_selection_value_calculations(
        model_probability=value.model_probability,
        decimal_odds=value.decimal_odds,
        raw_implied_probability=value.raw_implied_probability,
        normalized_implied_probability=value.normalized_implied_probability,
        overround=evaluation.overround,
        edge=value.edge,
        expected_value=value.expected_value,
        complete_market_raw_total=complete_market_raw_total,
    )
    lineage = prediction.lineage
    if lineage.feature_row_id != prediction.canonical_event_id:
        raise OpportunityError("feature_row_id must match canonical_event_id")
    if evaluation.mode is QuoteEvaluationMode.LIVE_SAFE:
        for field_name, field_value in (
            ("model_artifact_id", lineage.model_artifact_id),
            ("model_checksum_sha256", lineage.model_checksum_sha256),
            ("model_specification_version", lineage.model_specification_version),
            ("feature_artifact_id", lineage.feature_artifact_id),
            ("feature_manifest_checksum_sha256", lineage.feature_manifest_checksum_sha256),
            ("feature_specification_version", lineage.feature_specification_version),
            ("feature_row_id", lineage.feature_row_id),
        ):
            if not field_value:
                raise OpportunityError(f"live-safe opportunity requires {field_name}")
        try:
            validate_sha256_checksum(lineage.model_checksum_sha256)
            validate_sha256_checksum(lineage.feature_manifest_checksum_sha256)
        except Exception as exc:
            raise OpportunityError("opportunity lineage checksum is malformed") from exc
    keys = dependency_keys or frozenset(
        {
            f"sport:{value.selection.sport_code}",
            f"event:{prediction.canonical_event_id}",
        }
    )
    participants = participant_ids or frozenset()
    opportunity_id = derive_opportunity_id(
        evaluation_version=evaluation.evaluation_version,
        mode=evaluation.mode,
        prediction_id=prediction.prediction_id,
        quote_observation_id=quote_observation_id,
        selection_id=value.selection.selection_id,
        source_name=quote.source_name,
        provider_type=quote.provider_type,
        provider_id=quote.provider_id,
        decimal_odds=value.decimal_odds,
        decision_as_of_utc=decision_as_of,
    )
    return Opportunity(
        opportunity_id=opportunity_id,
        canonical_event_id=prediction.canonical_event_id,
        event_start_utc=prediction.event_start_utc,
        selection=value.selection,
        prediction_id=prediction.prediction_id,
        predicted_at_utc=prediction.predicted_at_utc,
        model_trained_through_date=lineage.trained_through_date,
        model_calibrated_through_date=lineage.calibrated_through_date,
        quote_observation_id=quote_observation_id,
        quoted_at_utc=quote.quoted_at_utc,
        source_observed_at_utc=quote.source_observed_at_utc,
        source_name=quote.source_name,
        provider_type=quote.provider_type,
        provider_id=quote.provider_id,
        evaluation_mode=evaluation.mode,
        decimal_odds=value.decimal_odds,
        model_probability=value.model_probability,
        raw_implied_probability=value.raw_implied_probability,
        normalized_implied_probability=value.normalized_implied_probability,
        overround=evaluation.overround,
        edge=value.edge,
        expected_value=value.expected_value,
        decision_as_of_utc=decision_as_of,
        model_artifact_id=lineage.model_artifact_id,
        model_checksum_sha256=lineage.model_checksum_sha256,
        model_specification_version=lineage.model_specification_version,
        feature_artifact_id=lineage.feature_artifact_id,
        feature_manifest_checksum_sha256=lineage.feature_manifest_checksum_sha256,
        feature_specification_version=lineage.feature_specification_version,
        feature_row_id=lineage.feature_row_id,
        dependency_keys=keys,
        participant_ids=participants,
        dependency_metadata_complete=dependency_metadata_complete,
        prediction_quality_passed=prediction.quality.production_eligible,
    )


def verify_opportunity_identity(opportunity: Opportunity) -> None:
    """Recompute and verify one opportunity's identity and calculations."""
    expected_id = derive_opportunity_id(
        evaluation_version="complete-market-value-v1",
        mode=opportunity.evaluation_mode,
        prediction_id=opportunity.prediction_id,
        quote_observation_id=opportunity.quote_observation_id,
        selection_id=opportunity.selection.selection_id,
        source_name=opportunity.source_name,
        provider_type=opportunity.provider_type,
        provider_id=opportunity.provider_id,
        decimal_odds=opportunity.decimal_odds,
        decision_as_of_utc=cast_datetime(opportunity.decision_as_of_utc),
    )
    if opportunity.opportunity_id != expected_id:
        raise OpportunityError("opportunity_id does not match canonical identity")
    complete_market_raw_total = opportunity.raw_implied_probability / (
        opportunity.normalized_implied_probability
    )
    verify_selection_value_calculations(
        model_probability=opportunity.model_probability,
        decimal_odds=opportunity.decimal_odds,
        raw_implied_probability=opportunity.raw_implied_probability,
        normalized_implied_probability=opportunity.normalized_implied_probability,
        overround=opportunity.overround,
        edge=opportunity.edge,
        expected_value=opportunity.expected_value,
        complete_market_raw_total=complete_market_raw_total,
    )


def cast_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise OpportunityError("decision_as_of_utc is required")
    return require_utc(value, field_name="decision_as_of_utc")

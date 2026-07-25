"""Canonical opportunity identity and verified construction."""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from sports_analytics.core.exceptions import OpportunityError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc
from sports_analytics.value.contracts import (
    MarketValueEvaluation,
    SelectionValue,
)
from sports_analytics.value.timing import compute_decision_as_of

if TYPE_CHECKING:
    from sports_analytics.opportunities.contracts import Opportunity

OPPORTUNITY_IDENTITY_VERSION: str = "opportunity-v2"
VALUE_CALCULATION_TOLERANCE: float = 1e-9


def derive_opportunity_id(*, payload: dict[str, JsonValue]) -> str:
    """Derive one content-addressed opportunity identity from a canonical payload."""
    return content_addressed_id(
        identity_type=OPPORTUNITY_IDENTITY_VERSION,
        payload=payload,
    )


def opportunity_identity_payload(opportunity: Opportunity) -> dict[str, JsonValue]:
    """Build the canonical opportunity identity payload from one opportunity."""
    decision_as_of = cast_datetime(opportunity.decision_as_of_utc)
    return {
        "identity_version": OPPORTUNITY_IDENTITY_VERSION,
        "evaluation_version": opportunity.evaluation_version,
        "canonical_event_id": opportunity.canonical_event_id,
        "event_start_utc": format_utc_timestamp(opportunity.event_start_utc),
        "selection": opportunity.selection.identity_payload(),
        "prediction_id": opportunity.prediction_id,
        "predicted_at_utc": format_utc_timestamp(opportunity.predicted_at_utc),
        "quote_series_id": opportunity.quote_series_id,
        "quote_observation_id": opportunity.quote_observation_id,
        "source_name": opportunity.source_name,
        "provider_type": opportunity.provider_type,
        "provider_id": opportunity.provider_id,
        "evaluation_mode": opportunity.evaluation_mode.value,
        "quoted_at_utc": (
            None
            if opportunity.quoted_at_utc is None
            else format_utc_timestamp(opportunity.quoted_at_utc)
        ),
        "source_observed_at_utc": format_utc_timestamp(opportunity.source_observed_at_utc),
        "decision_as_of_utc": format_utc_timestamp(decision_as_of),
        "decimal_odds": format(opportunity.decimal_odds, "f"),
        "model_probability": opportunity.model_probability,
        "raw_implied_probability": opportunity.raw_implied_probability,
        "normalized_implied_probability": opportunity.normalized_implied_probability,
        "overround": opportunity.overround,
        "edge": opportunity.edge,
        "expected_value": opportunity.expected_value,
        "model_artifact_id": opportunity.model_artifact_id,
        "model_checksum_sha256": opportunity.model_checksum_sha256,
        "model_specification_version": opportunity.model_specification_version,
        "feature_artifact_id": opportunity.feature_artifact_id,
        "feature_manifest_checksum_sha256": opportunity.feature_manifest_checksum_sha256,
        "feature_specification_version": opportunity.feature_specification_version,
        "feature_row_id": opportunity.feature_row_id,
        "dependency_keys": cast(list[JsonValue], sorted(opportunity.dependency_keys)),
        "participant_ids": cast(list[JsonValue], sorted(opportunity.participant_ids)),
        "dependency_metadata_complete": opportunity.dependency_metadata_complete,
        "prediction_quality_passed": opportunity.prediction_quality_passed,
    }


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
) -> None:
    """Recompute and verify one selection's value calculations."""
    for field_name, value in (
        ("model_probability", model_probability),
        ("raw_implied_probability", raw_implied_probability),
        ("normalized_implied_probability", normalized_implied_probability),
        ("overround", overround),
        ("edge", edge),
        ("expected_value", expected_value),
    ):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise OpportunityError(f"{field_name} must be numeric")
        if not math.isfinite(float(value)):
            raise OpportunityError(f"{field_name} must be finite")
    if not 0.0 <= model_probability <= 1.0:
        raise OpportunityError("model probability must lie in [0, 1]")
    if isinstance(overround, bool) or not math.isfinite(overround) or overround < 0.0:
        raise OpportunityError("overround must be finite and non-negative")
    complete_market_raw_total = 1.0 + overround
    if complete_market_raw_total <= 0.0:
        raise OpportunityError("complete market raw total must be positive")
    odds = float(decimal_odds)
    if not math.isfinite(odds) or odds <= 1.0:
        raise OpportunityError("decimal odds must be finite and >1")
    expected_raw = 1.0 / odds
    if not math.isfinite(expected_raw) or expected_raw <= 0.0:
        raise OpportunityError("raw implied probability must be positive and finite")
    if abs(raw_implied_probability - expected_raw) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("raw implied probability does not match decimal odds")
    if normalized_implied_probability <= 0.0 or normalized_implied_probability > 1.0:
        raise OpportunityError("normalized implied probability must be in (0, 1]")
    expected_normalized = expected_raw / complete_market_raw_total
    if abs(normalized_implied_probability - expected_normalized) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("normalized implied probability is inconsistent with overround")
    expected_edge = model_probability - expected_normalized
    if abs(edge - expected_edge) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("probability edge is inconsistent with model and market")
    expected_ev = model_probability * odds - 1.0
    if abs(expected_value - expected_ev) > VALUE_CALCULATION_TOLERANCE:
        raise OpportunityError("expected value is inconsistent with model probability and odds")


def build_opportunity_from_evaluation(
    evaluation: MarketValueEvaluation,
    value: SelectionValue,
    *,
    quote_observation_id: str,
    quote_series_id: str,
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
    verify_selection_value_calculations(
        model_probability=value.model_probability,
        decimal_odds=value.decimal_odds,
        raw_implied_probability=value.raw_implied_probability,
        normalized_implied_probability=value.normalized_implied_probability,
        overround=evaluation.overround,
        edge=value.edge,
        expected_value=value.expected_value,
    )
    lineage = prediction.lineage
    if lineage.feature_row_id != prediction.canonical_event_id:
        raise OpportunityError("feature_row_id must match canonical_event_id")
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
            raise OpportunityError(f"opportunity requires complete lineage: {field_name}")
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
    payload: dict[str, JsonValue] = {
        "identity_version": OPPORTUNITY_IDENTITY_VERSION,
        "evaluation_version": evaluation.evaluation_version,
        "canonical_event_id": prediction.canonical_event_id,
        "event_start_utc": format_utc_timestamp(prediction.event_start_utc),
        "selection": value.selection.identity_payload(),
        "prediction_id": prediction.prediction_id,
        "predicted_at_utc": format_utc_timestamp(prediction.predicted_at_utc),
        "quote_series_id": quote_series_id,
        "quote_observation_id": quote_observation_id,
        "source_name": quote.source_name,
        "provider_type": quote.provider_type,
        "provider_id": quote.provider_id,
        "evaluation_mode": evaluation.mode.value,
        "quoted_at_utc": (
            None if quote.quoted_at_utc is None else format_utc_timestamp(quote.quoted_at_utc)
        ),
        "source_observed_at_utc": format_utc_timestamp(quote.source_observed_at_utc),
        "decision_as_of_utc": format_utc_timestamp(decision_as_of),
        "decimal_odds": format(value.decimal_odds, "f"),
        "model_probability": value.model_probability,
        "raw_implied_probability": value.raw_implied_probability,
        "normalized_implied_probability": value.normalized_implied_probability,
        "overround": evaluation.overround,
        "edge": value.edge,
        "expected_value": value.expected_value,
        "model_artifact_id": lineage.model_artifact_id,
        "model_checksum_sha256": lineage.model_checksum_sha256,
        "model_specification_version": lineage.model_specification_version,
        "feature_artifact_id": lineage.feature_artifact_id,
        "feature_manifest_checksum_sha256": lineage.feature_manifest_checksum_sha256,
        "feature_specification_version": lineage.feature_specification_version,
        "feature_row_id": lineage.feature_row_id,
        "dependency_keys": cast(list[JsonValue], sorted(keys)),
        "participant_ids": cast(list[JsonValue], sorted(participants)),
        "dependency_metadata_complete": dependency_metadata_complete,
        "prediction_quality_passed": prediction.quality.production_eligible,
    }
    return Opportunity(
        opportunity_id=derive_opportunity_id(payload=payload),
        canonical_event_id=prediction.canonical_event_id,
        event_start_utc=prediction.event_start_utc,
        selection=value.selection,
        prediction_id=prediction.prediction_id,
        predicted_at_utc=prediction.predicted_at_utc,
        model_trained_through_date=lineage.trained_through_date,
        model_calibrated_through_date=lineage.calibrated_through_date,
        quote_observation_id=quote_observation_id,
        quote_series_id=quote_series_id,
        quoted_at_utc=quote.quoted_at_utc,
        source_observed_at_utc=quote.source_observed_at_utc,
        source_name=quote.source_name,
        provider_type=quote.provider_type,
        provider_id=quote.provider_id,
        evaluation_mode=evaluation.mode,
        evaluation_version=evaluation.evaluation_version,
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
    expected_id = derive_opportunity_id(payload=opportunity_identity_payload(opportunity))
    if opportunity.opportunity_id != expected_id:
        raise OpportunityError("opportunity_id does not match canonical identity")
    verify_selection_value_calculations(
        model_probability=opportunity.model_probability,
        decimal_odds=opportunity.decimal_odds,
        raw_implied_probability=opportunity.raw_implied_probability,
        normalized_implied_probability=opportunity.normalized_implied_probability,
        overround=opportunity.overround,
        edge=opportunity.edge,
        expected_value=opportunity.expected_value,
    )


def cast_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise OpportunityError("decision_as_of_utc is required")
    return require_utc(value, field_name="decision_as_of_utc")

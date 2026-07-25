"""Verified analysis artifact publication workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sports_analytics.artifact_schemas import DATASET_SCHEMA_VERSIONS
from sports_analytics.artifacts import (
    TypedAnalyticalArtifact,
    load_typed_analytical_artifact,
    write_typed_analytical_artifact,
)
from sports_analytics.combinations.builder import build_combinations
from sports_analytics.combinations.contracts import Combination, CombinationRules
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityDecision,
    OpportunityFilter,
    OpportunityRejection,
    filter_and_rank_opportunities,
    opportunities_from_evaluation,
)
from sports_analytics.opportunities.identity import derive_evaluation_id
from sports_analytics.predictions.contracts import MarketPrediction
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    MarketValueEvaluation,
    QuoteEvaluationMode,
    evaluate_complete_market,
)

ANALYSIS_ARTIFACT_SCHEMA: str = "analysis-v1"


@dataclass(frozen=True, slots=True)
class AnalysisPublicationRequest:
    """Inputs for one verified analysis artifact publication."""

    prediction: MarketPrediction
    quote: CompleteMarketQuote
    mode: QuoteEvaluationMode
    filters: OpportunityFilter
    combination_rules: CombinationRules | None = None
    relative_directory: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedAnalysisArtifact:
    """Verified analysis artifact publication result."""

    artifact: TypedAnalyticalArtifact
    artifact_id: str
    checksum_sha256: str
    relative_directory: str


def publish_analysis_artifact(
    *,
    paths: RuntimePaths,
    request: AnalysisPublicationRequest,
) -> PublishedAnalysisArtifact:
    """Calculate, filter, combine, publish atomically, and reload one analysis artifact."""
    evaluation = evaluate_complete_market(
        prediction=request.prediction,
        quote=request.quote,
        mode=request.mode,
    )
    opportunities = opportunities_from_evaluation(evaluation)
    search = filter_and_rank_opportunities(opportunities, filters=request.filters)
    combinations: tuple[Combination, ...] = ()
    combination_rejections: tuple[dict[str, JsonValue], ...] = ()
    if request.combination_rules is not None:
        build = build_combinations(search.accepted, rules=request.combination_rules)
        combinations = build.combinations
        combination_rejections = tuple(
            {
                "rejection_id": content_addressed_id(
                    identity_type="analysis-combination-rejection-v1",
                    payload={
                        "opportunity_ids": list(item.opportunity_ids),
                        "reason": item.reason,
                    },
                ),
                "opportunity_ids": list(item.opportunity_ids),
                "reason": item.reason,
            }
            for item in build.rejections
        )
    datasets = _analysis_datasets(
        prediction=request.prediction,
        evaluation=evaluation,
        opportunities=opportunities,
        search_accepted=search.accepted,
        search_decisions=search.decisions,
        search_rejected=search.rejected,
        combinations=combinations,
        combination_rejections=combination_rejections,
        filters=request.filters,
    )
    relative = request.relative_directory or (
        f"analysis/{ANALYSIS_ARTIFACT_SCHEMA}/{datasets['predictions'][0]['prediction_id']}"
    )
    artifact = write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        artifact_kind="analysis",
        schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        datasets=datasets,
        dataset_schema_versions=DATASET_SCHEMA_VERSIONS,
    )
    verified = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=artifact.relative_directory,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=artifact.checksum_sha256,
        expected_artifact_id=artifact.artifact_id,
    )
    return PublishedAnalysisArtifact(
        artifact=verified,
        artifact_id=verified.artifact_id,
        checksum_sha256=verified.checksum_sha256,
        relative_directory=verified.relative_directory,
    )


def _analysis_datasets(
    *,
    prediction: MarketPrediction,
    evaluation: MarketValueEvaluation,
    opportunities: tuple[Opportunity, ...],
    search_accepted: tuple[Opportunity, ...],
    search_decisions: tuple[OpportunityDecision, ...],
    search_rejected: tuple[OpportunityRejection, ...],
    combinations: tuple[Combination, ...],
    combination_rejections: tuple[dict[str, JsonValue], ...],
    filters: OpportunityFilter,
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    prediction_row = {
        "prediction_id": prediction.prediction_id,
        "schema_version": DATASET_SCHEMA_VERSIONS["predictions"],
        "canonical_event_id": prediction.canonical_event_id,
        "event_start_utc": format_utc_timestamp(prediction.event_start_utc),
        "predicted_at_utc": format_utc_timestamp(prediction.predicted_at_utc),
        "ordered_selection_ids": list(prediction.ordered_selection_ids),
        "probabilities": [
            {
                "selection_id": item.selection.selection_id,
                "probability": item.probability,
            }
            for item in prediction.probabilities
        ],
    }
    evaluation_rows: list[dict[str, JsonValue]] = []
    opportunity_rows: list[dict[str, JsonValue]] = []
    priced = {item.selection.selection_id: item for item in evaluation.quote.selections}
    for value in evaluation.selections:
        quote_selection = priced[value.selection.selection_id]
        evaluation_id = derive_evaluation_id(
            prediction_id=prediction.prediction_id,
            quote_observation_id=quote_selection.quote_observation_id,
            selection_id=value.selection.selection_id,
        )
        evaluation_rows.append(
            {
                "evaluation_id": evaluation_id,
                "schema_version": DATASET_SCHEMA_VERSIONS["market_evaluations"],
                "prediction_id": prediction.prediction_id,
                "quote_observation_id": quote_selection.quote_observation_id,
                "selection_id": value.selection.selection_id,
                "expected_value": value.expected_value,
                "edge": value.edge,
                "raw_implied_probability": value.raw_implied_probability,
                "normalized_implied_probability": value.normalized_implied_probability,
                "overround": evaluation.overround,
            }
        )
    for opportunity in opportunities:
        opportunity_rows.append(
            {
                "opportunity_id": opportunity.opportunity_id,
                "schema_version": DATASET_SCHEMA_VERSIONS["opportunities"],
                "canonical_event_id": opportunity.canonical_event_id,
                "event_start_utc": format_utc_timestamp(opportunity.event_start_utc),
                "decision_as_of_utc": format_utc_timestamp(
                    cast(datetime, opportunity.decision_as_of_utc)
                ),
                "prediction_id": opportunity.prediction_id,
                "quote_observation_id": opportunity.quote_observation_id,
                "provider_id": opportunity.provider_id,
                "decimal_odds": format(opportunity.decimal_odds, "f"),
                "model_probability": opportunity.model_probability,
                "edge": opportunity.edge,
                "expected_value": opportunity.expected_value,
                "raw_implied_probability": opportunity.raw_implied_probability,
                "normalized_implied_probability": opportunity.normalized_implied_probability,
                "model_artifact_id": opportunity.model_artifact_id,
                "model_checksum_sha256": opportunity.model_checksum_sha256,
                "feature_artifact_id": opportunity.feature_artifact_id,
                "feature_manifest_checksum_sha256": opportunity.feature_manifest_checksum_sha256,
                "feature_row_id": opportunity.feature_row_id,
            }
        )
    decision_rows = tuple(
        {
            "opportunity_id": item.opportunity_id,
            "schema_version": DATASET_SCHEMA_VERSIONS["opportunity_decisions"],
            "filter_config_id": item.filter_config_id,
            "decision_as_of_utc": format_utc_timestamp(item.decision_as_of_utc),
            "eligible": item.eligible,
            "rejection_codes": [code.value for code in item.rejection_codes],
            "accepted_rank": item.accepted_rank,
        }
        for item in search_decisions
    )
    rejection_rows = tuple(
        {
            "rejection_id": content_addressed_id(
                identity_type="analysis-opportunity-rejection-v1",
                payload={
                    "opportunity_id": item.opportunity.opportunity_id,
                    "codes": [code.value for code in item.codes],
                    "filter_config_id": filters.filter_config_id,
                },
            ),
            "schema_version": DATASET_SCHEMA_VERSIONS["rejections"],
            "opportunity_id": item.opportunity.opportunity_id,
            "codes": [code.value for code in item.codes],
        }
        for item in search_rejected
    )
    combination_rows = tuple(
        {
            "combination_id": item.combination_id,
            "schema_version": DATASET_SCHEMA_VERSIONS["combinations"],
            "opportunity_ids": [leg.opportunity_id for leg in item.legs],
            "total_decimal_odds": format(item.total_decimal_odds, "f"),
            "joint_probability": item.joint_probability,
            "expected_value": item.expected_value,
            "common_decision_time_utc": format_utc_timestamp(item.common_information_time_utc),
            "earliest_event_start_utc": format_utc_timestamp(item.earliest_event_start_utc),
            "policy_id": item.policy_id,
        }
        for item in combinations
    )
    if not prediction_row["prediction_id"]:
        raise ArtifactError("analysis publication requires a verified prediction")
    return cast(
        dict[str, tuple[dict[str, JsonValue], ...]],
        {
            "predictions": (prediction_row,),
            "market_evaluations": tuple(evaluation_rows),
            "opportunity_decisions": decision_rows,
            "opportunities": tuple(opportunity_rows),
            "combinations": combination_rows,
            "rejections": rejection_rows + combination_rejections,
        },
    )

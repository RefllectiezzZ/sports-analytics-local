"""Verified analysis artifact publication workflow."""

from __future__ import annotations

from dataclasses import dataclass

from sports_analytics.artifact_schemas import DATASET_SCHEMA_VERSIONS
from sports_analytics.artifact_serializers import (
    build_analysis_datasets,
    derive_analysis_run_id,
    quote_fingerprint_from_quote,
)
from sports_analytics.artifacts import (
    TypedAnalyticalArtifact,
    load_typed_analytical_artifact,
    write_typed_analytical_artifact,
)
from sports_analytics.combinations.builder import CombinationRejection, build_combinations
from sports_analytics.combinations.contracts import Combination, CombinationRules
from sports_analytics.combinations.evidence import CombinationEvidenceMode
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityFilter,
    filter_and_rank_opportunities,
    opportunities_from_evaluation,
)
from sports_analytics.opportunities.dependency import MarketDependencyMetadata
from sports_analytics.predictions.contracts import MarketPrediction
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    MarketValueEvaluation,
    QuoteEvaluationMode,
    evaluate_complete_market,
)

ANALYSIS_ARTIFACT_SCHEMA: str = "analysis-v2"


@dataclass(frozen=True, slots=True)
class AnalysisMarketInput:
    """One verified prediction and complete quote pair with optional dependency metadata."""

    prediction: MarketPrediction
    quote: CompleteMarketQuote
    dependency_metadata: MarketDependencyMetadata | None = None


@dataclass(frozen=True, slots=True)
class AnalysisPublicationRequest:
    """Inputs for one verified multi-market analysis artifact publication."""

    markets: tuple[AnalysisMarketInput, ...]
    mode: QuoteEvaluationMode
    filters: OpportunityFilter
    combination_rules: CombinationRules | None = None
    provenance: PredictionProvenance = PredictionProvenance.SYNTHETIC_CONTRACT
    relative_directory: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedAnalysisArtifact:
    """Verified analysis artifact publication result."""

    artifact: TypedAnalyticalArtifact
    artifact_id: str
    checksum_sha256: str
    relative_directory: str
    analysis_run_id: str


def publish_analysis_artifact(
    *,
    paths: RuntimePaths,
    request: AnalysisPublicationRequest,
) -> PublishedAnalysisArtifact:
    """Calculate, filter, combine, publish atomically, and reload one analysis artifact."""
    if not request.markets:
        raise ArtifactError("analysis publication requires at least one market pair")
    if request.provenance is PredictionProvenance.HISTORICAL_REPLAY:
        for market in request.markets:
            if market.prediction.provenance is not PredictionProvenance.HISTORICAL_REPLAY:
                raise ArtifactError(
                    "historical-replay analysis requires verified historical predictions"
                )
    canonical_markets = _canonicalize_analysis_markets(request.markets)
    evaluations: list[MarketValueEvaluation] = []
    all_opportunities: list[Opportunity] = []
    predictions: list[MarketPrediction] = []
    for market in canonical_markets:
        if market.prediction.canonical_event_id != market.quote.canonical_event_id:
            raise ArtifactError("prediction and quote canonical_event_id must match")
        evaluation = evaluate_complete_market(
            prediction=market.prediction,
            quote=market.quote,
            mode=request.mode,
        )
        evaluations.append(evaluation)
        metadata_map = (
            None
            if market.dependency_metadata is None
            else market.dependency_metadata.by_selection_id
        )
        all_opportunities.extend(
            opportunities_from_evaluation(
                evaluation,
                dependency_metadata_by_selection=metadata_map,
            )
        )
        predictions.append(market.prediction)
    search = filter_and_rank_opportunities(tuple(all_opportunities), filters=request.filters)
    combinations: tuple[Combination, ...] = ()
    combination_rejections: tuple[CombinationRejection, ...] = ()
    builder_truncated = False
    if request.combination_rules is not None:
        build = build_combinations(
            search.accepted,
            rules=request.combination_rules,
            evidence_mode=_combination_evidence_mode(request.provenance),
        )
        combinations = build.combinations
        combination_rejections = build.rejections
        builder_truncated = build.truncated
    market_fingerprints = tuple(
        (market.prediction, quote_fingerprint_from_quote(market.quote))
        for market in canonical_markets
    )
    analysis_run_id = derive_analysis_run_id(
        markets=tuple((prediction, fingerprint) for prediction, fingerprint in market_fingerprints),
        mode=request.mode.value,
        filters=request.filters,
        combination_rules_id=(
            None if request.combination_rules is None else request.combination_rules.policy_id
        ),
        provenance=request.provenance.value,
    )
    datasets = build_analysis_datasets(
        predictions=tuple(predictions),
        evaluations=tuple(evaluations),
        opportunities=tuple(all_opportunities),
        decisions=search.decisions,
        opportunity_rejections=search.rejected,
        combinations=combinations,
        combination_rejections=combination_rejections,
        filters=request.filters,
        combination_policy_id=(
            None if request.combination_rules is None else request.combination_rules.policy_id
        ),
        provenance=request.provenance.value,
        builder_truncated=builder_truncated,
    )
    relative = request.relative_directory or (
        f"analysis/{ANALYSIS_ARTIFACT_SCHEMA}/{analysis_run_id}"
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
        analysis_run_id=analysis_run_id,
    )


def _canonicalize_analysis_markets(
    markets: tuple[AnalysisMarketInput, ...],
) -> tuple[AnalysisMarketInput, ...]:
    return tuple(
        sorted(
            markets,
            key=lambda item: (
                item.prediction.prediction_id,
                quote_fingerprint_from_quote(item.quote),
            ),
        )
    )


def _combination_evidence_mode(
    provenance: PredictionProvenance,
) -> CombinationEvidenceMode:
    if provenance is PredictionProvenance.SYNTHETIC_CONTRACT:
        return CombinationEvidenceMode.SYNTHETIC_CONTRACT
    return CombinationEvidenceMode.TRUSTED_VERIFIED

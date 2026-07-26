"""Canonical typed dataset serializers shared by analysis and backtest artifacts."""

from __future__ import annotations

from decimal import Decimal

from sports_analytics.artifact_schemas import (
    AGGREGATE_METRICS_SCHEMA_VERSION,
    COMBINATIONS_SCHEMA_VERSION,
    FOLD_METRICS_SCHEMA_VERSION,
    MARKET_EVALUATIONS_SCHEMA_VERSION,
    OPPORTUNITIES_SCHEMA_VERSION,
    OPPORTUNITY_DECISIONS_SCHEMA_VERSION,
    PREDICTIONS_SCHEMA_VERSION,
    REJECTIONS_SCHEMA_VERSION,
    SETTLEMENTS_SCHEMA_VERSION,
)
from sports_analytics.backtesting.contracts import BacktestResult, SettledBet
from sports_analytics.combinations.builder import CombinationRejection
from sports_analytics.combinations.contracts import Combination
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityDecision,
    OpportunityFilter,
    OpportunityRejection,
)
from sports_analytics.opportunities.identity import (
    derive_evaluation_id,
    opportunity_identity_payload,
)
from sports_analytics.predictions.contracts import MarketPrediction
from sports_analytics.value.contracts import MarketValueEvaluation, SelectionValue

REJECTION_KIND_OPPORTUNITY_FILTER = "opportunity-filter"
REJECTION_KIND_COMBINATION_BUILDER = "combination-builder"


def serialize_prediction_row(
    prediction: MarketPrediction,
    *,
    provenance: str | None = None,
) -> dict[str, JsonValue]:
    """Serialize one authoritative prediction dataset row."""
    lineage = prediction.lineage
    quality = prediction.quality
    row_provenance = provenance or prediction.provenance.value
    return {
        "prediction_id": prediction.prediction_id,
        "schema_version": PREDICTIONS_SCHEMA_VERSION,
        "canonical_event_id": prediction.canonical_event_id,
        "event_start_utc": format_utc_timestamp(prediction.event_start_utc),
        "predicted_at_utc": format_utc_timestamp(prediction.predicted_at_utc),
        "feature_available_at_utc": format_utc_timestamp(prediction.feature_available_at_utc),
        "provenance": row_provenance,
        "ordered_selection_ids": list(prediction.ordered_selection_ids),
        "probabilities": [
            {
                "selection": item.selection.identity_payload(),
                "selection_id": item.selection.selection_id,
                "probability": item.probability,
            }
            for item in prediction.probabilities
        ],
        "quality": {
            "calibrated": quality.calibrated,
            "model_artifact_verified": quality.model_artifact_verified,
            "feature_artifact_verified": quality.feature_artifact_verified,
            "sufficient_history": quality.sufficient_history,
            "data_quality_passed": quality.data_quality_passed,
        },
        "lineage": {
            "model_artifact_id": lineage.model_artifact_id,
            "model_checksum_sha256": lineage.model_checksum_sha256,
            "model_specification_version": lineage.model_specification_version,
            "feature_artifact_id": lineage.feature_artifact_id,
            "feature_manifest_checksum_sha256": lineage.feature_manifest_checksum_sha256,
            "feature_specification_version": lineage.feature_specification_version,
            "feature_row_id": lineage.feature_row_id,
            "trained_through_date": lineage.trained_through_date.isoformat(),
            "calibrated_through_date": lineage.calibrated_through_date.isoformat(),
            "input_snapshots": [
                {
                    "snapshot_id": item.snapshot_id,
                    "manifest_checksum_sha256": item.manifest_checksum_sha256,
                    "schema_version": item.schema_version,
                    "source_name": item.source_name,
                }
                for item in lineage.input_snapshots
            ],
        },
    }


def serialize_market_evaluation_row(
    *,
    evaluation: MarketValueEvaluation,
    value: SelectionValue,
    quote_observation_id: str,
    quote_series_id: str,
) -> dict[str, JsonValue]:
    """Serialize one market evaluation row with enough data to recompute formulas."""
    prediction = evaluation.prediction
    quote = evaluation.quote
    complete_market_raw_total = 1.0 + evaluation.overround
    evaluation_id = derive_evaluation_id(
        evaluation_version=evaluation.evaluation_version,
        prediction_id=prediction.prediction_id,
        quote_observation_id=quote_observation_id,
        quote_series_id=quote_series_id,
        selection_id=value.selection.selection_id,
        source_name=quote.source_name,
        provider_type=quote.provider_type,
        provider_id=quote.provider_id,
        evaluation_mode=evaluation.mode.value,
        decimal_odds=format(value.decimal_odds, "f"),
        model_probability=value.model_probability,
        raw_implied_probability=value.raw_implied_probability,
        complete_market_raw_total=complete_market_raw_total,
        overround=evaluation.overround,
        normalized_implied_probability=value.normalized_implied_probability,
        edge=value.edge,
        expected_value=value.expected_value,
    )
    return {
        "evaluation_id": evaluation_id,
        "schema_version": MARKET_EVALUATIONS_SCHEMA_VERSION,
        "evaluation_version": evaluation.evaluation_version,
        "prediction_id": prediction.prediction_id,
        "quote_observation_id": quote_observation_id,
        "quote_series_id": quote_series_id,
        "selection_id": value.selection.selection_id,
        "selection": value.selection.identity_payload(),
        "source_name": quote.source_name,
        "provider_type": quote.provider_type,
        "provider_id": quote.provider_id,
        "evaluation_mode": evaluation.mode.value,
        "decimal_odds": format(value.decimal_odds, "f"),
        "model_probability": value.model_probability,
        "raw_implied_probability": value.raw_implied_probability,
        "complete_market_raw_total": complete_market_raw_total,
        "overround": evaluation.overround,
        "normalized_implied_probability": value.normalized_implied_probability,
        "edge": value.edge,
        "expected_value": value.expected_value,
    }


def serialize_opportunity_row(opportunity: Opportunity) -> dict[str, JsonValue]:
    """Serialize one opportunity row including the full identity payload."""
    payload = opportunity_identity_payload(opportunity)
    row = dict(payload)
    row["schema_version"] = OPPORTUNITIES_SCHEMA_VERSION
    row["opportunity_id"] = opportunity.opportunity_id
    row["model_trained_through_date"] = opportunity.model_trained_through_date.isoformat()
    row["model_calibrated_through_date"] = opportunity.model_calibrated_through_date.isoformat()
    return row


def serialize_opportunity_decision_row(decision: OpportunityDecision) -> dict[str, JsonValue]:
    """Serialize one opportunity decision row."""
    return {
        "opportunity_id": decision.opportunity_id,
        "schema_version": OPPORTUNITY_DECISIONS_SCHEMA_VERSION,
        "filter_config_id": decision.filter_config_id,
        "decision_as_of_utc": format_utc_timestamp(decision.decision_as_of_utc),
        "eligible": decision.eligible,
        "rejection_codes": [code.value for code in decision.rejection_codes],
        "accepted_rank": decision.accepted_rank,
    }


def serialize_combination_row(combination: Combination) -> dict[str, JsonValue]:
    """Serialize one combination row with dependency assessments."""
    return {
        "combination_id": combination.combination_id,
        "schema_version": COMBINATIONS_SCHEMA_VERSION,
        "policy_id": combination.policy_id,
        "policy_version": combination.policy_version,
        "opportunity_ids": [leg.opportunity_id for leg in combination.legs],
        "dependencies": [
            {
                "left_opportunity_id": item.left_opportunity_id,
                "right_opportunity_id": item.right_opportunity_id,
                "classification": item.classification.value,
                "reason": item.reason,
            }
            for item in combination.dependencies
        ],
        "total_decimal_odds": format(combination.total_decimal_odds, "f"),
        "joint_probability": combination.joint_probability,
        "expected_value": combination.expected_value,
        "common_decision_time_utc": format_utc_timestamp(combination.common_information_time_utc),
        "earliest_event_start_utc": format_utc_timestamp(combination.earliest_event_start_utc),
        "latest_event_start_utc": format_utc_timestamp(combination.latest_event_start_utc),
        "eligible": combination.eligible,
        "rejection_reasons": list(combination.rejection_reasons),
    }


def serialize_opportunity_filter_rejection_row(
    rejection: OpportunityRejection,
    *,
    filter_config_id: str,
    strategy_id: str | None = None,
) -> dict[str, JsonValue]:
    """Serialize one opportunity-filter rejection row."""
    payload: dict[str, JsonValue] = {
        "opportunity_id": rejection.opportunity.opportunity_id,
        "filter_config_id": filter_config_id,
        "codes": [code.value for code in rejection.codes],
    }
    if strategy_id is not None:
        payload["strategy_id"] = strategy_id
    rejection_id = content_addressed_id(
        identity_type="opportunity-filter-rejection-v1",
        payload={
            "rejection_kind": REJECTION_KIND_OPPORTUNITY_FILTER,
            **payload,
        },
    )
    return {
        "rejection_id": rejection_id,
        "schema_version": REJECTIONS_SCHEMA_VERSION,
        "rejection_kind": REJECTION_KIND_OPPORTUNITY_FILTER,
        **payload,
    }


def serialize_combination_builder_rejection_row(
    rejection: CombinationRejection,
    *,
    policy_id: str,
    truncated: bool = False,
) -> dict[str, JsonValue]:
    """Serialize one combination-builder rejection row."""
    code = _combination_rejection_code(rejection.reason)
    payload: dict[str, JsonValue] = {
        "opportunity_ids": list(rejection.opportunity_ids),
        "rejection_code": code,
        "reason": rejection.reason,
        "policy_id": policy_id,
        "builder_truncated": truncated,
    }
    rejection_id = content_addressed_id(
        identity_type="combination-builder-rejection-v1",
        payload={
            "rejection_kind": REJECTION_KIND_COMBINATION_BUILDER,
            **payload,
        },
    )
    return {
        "rejection_id": rejection_id,
        "schema_version": REJECTIONS_SCHEMA_VERSION,
        "rejection_kind": REJECTION_KIND_COMBINATION_BUILDER,
        **payload,
    }


def serialize_settlement_row(
    bet: SettledBet,
    *,
    strategy_id: str | None = None,
    combination_id: str | None = None,
) -> dict[str, JsonValue]:
    """Serialize one flat-unit settlement row."""
    returned_units = bet.profit_units + bet.stake_units
    row: dict[str, JsonValue] = {
        "bet_id": bet.bet_id,
        "schema_version": SETTLEMENTS_SCHEMA_VERSION,
        "fold_id": bet.fold_id,
        "kind": bet.kind.value,
        "opportunity_ids": list(bet.opportunity_ids),
        "decimal_odds": format(bet.decimal_odds, "f"),
        "result": bet.result.value,
        "stake_units": "1",
        "returned_units": format(returned_units, "f"),
        "profit_units": format(bet.profit_units, "f"),
        "event_start_utc": (
            None if bet.event_start_utc is None else format_utc_timestamp(bet.event_start_utc)
        ),
    }
    if strategy_id is not None:
        row["strategy_id"] = strategy_id
    if combination_id is not None:
        row["combination_id"] = combination_id
    return row


def serialize_fold_metrics_row(
    *,
    fold_id: str,
    sample_size: int,
    accepted_single_count: int,
    accepted_combination_count: int,
    net_profit_units: Decimal,
) -> dict[str, JsonValue]:
    """Serialize one fold metrics row."""
    return {
        "fold_id": fold_id,
        "schema_version": FOLD_METRICS_SCHEMA_VERSION,
        "sample_size": sample_size,
        "accepted_single_count": accepted_single_count,
        "accepted_combination_count": accepted_combination_count,
        "net_profit_units": format(net_profit_units, "f"),
    }


def serialize_aggregate_metrics_row(
    *,
    backtest_id: str,
    result: BacktestResult,
    feature_artifact_id: str,
    feature_manifest_checksum_sha256: str,
    input_snapshots: tuple[dict[str, JsonValue], ...],
    random_seed: int,
    test_event_count: int,
    complete_quote_event_count: int,
    quote_coverage: float,
) -> dict[str, JsonValue]:
    """Serialize the complete aggregate metrics payload."""
    metrics = result.metrics
    return {
        "metric_id": "aggregate",
        "schema_version": AGGREGATE_METRICS_SCHEMA_VERSION,
        "backtest_id": backtest_id,
        "decision_run_id": result.decision_run_id,
        "mode": result.mode.value,
        "strategy_id": result.strategy_id,
        "feature_artifact_id": feature_artifact_id,
        "feature_manifest_checksum_sha256": feature_manifest_checksum_sha256,
        "input_snapshots": list(input_snapshots),
        "random_seed": random_seed,
        "test_event_count": test_event_count,
        "complete_quote_event_count": complete_quote_event_count,
        "quote_coverage": quote_coverage,
        "candidate_count": metrics.candidate_count,
        "rejection_count": metrics.rejection_count,
        "accepted_single_count": metrics.accepted_single_count,
        "accepted_combination_count": metrics.accepted_combination_count,
        "bet_count": metrics.bet_count,
        "win_count": metrics.win_count,
        "loss_count": metrics.loss_count,
        "push_count": metrics.push_count,
        "void_count": metrics.void_count,
        "staked_units": format(metrics.staked_units, "f"),
        "returned_units": format(metrics.returned_units, "f"),
        "gross_return_units": format(metrics.gross_return_units, "f"),
        "net_profit_units": format(metrics.net_profit_units, "f"),
        "roi": metrics.roi,
        "hit_rate": metrics.hit_rate,
        "average_decimal_odds": metrics.average_decimal_odds,
        "maximum_drawdown_units": format(metrics.maximum_drawdown_units, "f"),
        "cumulative_profit_units": [
            format(value, "f") for value in metrics.cumulative_profit_units
        ],
        "average_model_probability": metrics.average_model_probability,
        "average_edge": metrics.average_edge,
        "average_expected_value": metrics.average_expected_value,
        "all_prediction_count": metrics.all_prediction_count,
        "selected_prediction_count": metrics.selected_prediction_count,
        "all_log_loss": metrics.all_log_loss,
        "all_multiclass_brier_score": metrics.all_multiclass_brier_score,
        "selected_log_loss": metrics.selected_log_loss,
        "selected_multiclass_brier_score": metrics.selected_multiclass_brier_score,
        "rejection_counts_by_reason": [
            [reason, count, sample_size]
            for reason, count, sample_size in metrics.rejection_counts_by_reason
        ],
        "disclaimer": result.disclaimer,
    }


def derive_analysis_run_id(
    *,
    markets: tuple[tuple[MarketPrediction, str], ...],
    mode: str,
    filters: OpportunityFilter,
    combination_rules_id: str | None,
    provenance: str,
) -> str:
    """Derive one content-addressed analysis run identity from inputs and strategy."""
    canonical_markets = sorted(
        markets,
        key=lambda item: (item[0].prediction_id, item[1]),
    )
    return content_addressed_id(
        identity_type="analysis-run-v1",
        payload={
            "provenance": provenance,
            "mode": mode,
            "filter_config_id": filters.filter_config_id,
            "combination_policy_id": combination_rules_id,
            "markets": [
                {
                    "prediction_id": prediction.prediction_id,
                    "quote_fingerprint": quote_fingerprint,
                }
                for prediction, quote_fingerprint in canonical_markets
            ],
        },
    )


def quote_fingerprint_from_quote(quote: object) -> str:
    """Derive a stable quote fingerprint for analysis run identity."""
    from sports_analytics.value.contracts import CompleteMarketQuote

    if not isinstance(quote, CompleteMarketQuote):
        raise TypeError("quote must be a CompleteMarketQuote")
    return content_addressed_id(
        identity_type="complete-market-quote-v2",
        payload={
            "canonical_event_id": quote.canonical_event_id,
            "source_name": quote.source_name,
            "provider_type": quote.provider_type,
            "provider_id": quote.provider_id,
            "quote_phase": quote.quote_phase,
            "quote_timestamp_precision": quote.quote_timestamp_precision,
            "source_observed_at_utc": format_utc_timestamp(quote.source_observed_at_utc),
            "quoted_at_utc": (
                None if quote.quoted_at_utc is None else format_utc_timestamp(quote.quoted_at_utc)
            ),
            "quote_valid_from_utc": (
                None
                if quote.quote_valid_from_utc is None
                else format_utc_timestamp(quote.quote_valid_from_utc)
            ),
            "quote_valid_to_utc": (
                None
                if quote.quote_valid_to_utc is None
                else format_utc_timestamp(quote.quote_valid_to_utc)
            ),
            "selections": [
                {
                    "selection": item.selection.identity_payload(),
                    "selection_id": item.selection.selection_id,
                    "quote_series_id": item.quote_series_id,
                    "quote_observation_id": item.quote_observation_id,
                    "decimal_odds": format(item.decimal_odds, "f"),
                }
                for item in quote.selections
            ],
        },
    )


def build_analysis_datasets(
    *,
    predictions: tuple[MarketPrediction, ...],
    evaluations: tuple[MarketValueEvaluation, ...],
    opportunities: tuple[Opportunity, ...],
    decisions: tuple[OpportunityDecision, ...],
    opportunity_rejections: tuple[OpportunityRejection, ...],
    combinations: tuple[Combination, ...],
    combination_rejections: tuple[CombinationRejection, ...],
    filters: OpportunityFilter,
    combination_policy_id: str | None,
    provenance: str,
    builder_truncated: bool = False,
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    """Build complete analysis artifact datasets using canonical serializers."""
    prediction_rows = tuple(
        serialize_prediction_row(prediction, provenance=provenance) for prediction in predictions
    )
    evaluation_rows: list[dict[str, JsonValue]] = []
    for evaluation in evaluations:
        priced = {item.selection.selection_id: item for item in evaluation.quote.selections}
        for value in evaluation.selections:
            quote_selection = priced[value.selection.selection_id]
            evaluation_rows.append(
                serialize_market_evaluation_row(
                    evaluation=evaluation,
                    value=value,
                    quote_observation_id=quote_selection.quote_observation_id,
                    quote_series_id=quote_selection.quote_series_id,
                )
            )
    return {
        "predictions": prediction_rows,
        "market_evaluations": tuple(evaluation_rows),
        "opportunities": tuple(serialize_opportunity_row(item) for item in opportunities),
        "opportunity_decisions": tuple(
            serialize_opportunity_decision_row(item) for item in decisions
        ),
        "combinations": tuple(serialize_combination_row(item) for item in combinations),
        "rejections": tuple(
            serialize_opportunity_filter_rejection_row(
                item,
                filter_config_id=filters.filter_config_id,
            )
            for item in opportunity_rejections
        )
        + tuple(
            serialize_combination_builder_rejection_row(
                item,
                policy_id=combination_policy_id or "",
                truncated=builder_truncated,
            )
            for item in combination_rejections
        ),
    }


def build_backtest_datasets(
    *,
    result: BacktestResult,
    predictions: tuple[MarketPrediction, ...],
    evaluations: tuple[MarketValueEvaluation, ...],
    feature_artifact_id: str,
    feature_manifest_checksum_sha256: str,
    input_snapshots: tuple[dict[str, JsonValue], ...],
    random_seed: int,
    test_event_count: int,
    complete_quote_event_count: int,
    quote_coverage: float,
    provenance: str = "historical-replay",
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    """Build complete backtest artifact datasets using canonical serializers."""
    evaluation_rows: list[dict[str, JsonValue]] = []
    for evaluation in evaluations:
        priced = {item.selection.selection_id: item for item in evaluation.quote.selections}
        for value in evaluation.selections:
            quote_selection = priced[value.selection.selection_id]
            evaluation_rows.append(
                serialize_market_evaluation_row(
                    evaluation=evaluation,
                    value=value,
                    quote_observation_id=quote_selection.quote_observation_id,
                    quote_series_id=quote_selection.quote_series_id,
                )
            )
    opportunity_rows = tuple(
        serialize_opportunity_row(item.opportunity) for item in result.candidates
    )
    seen: set[str] = set()
    prediction_rows_list: list[dict[str, JsonValue]] = []
    for prediction in predictions:
        if prediction.prediction_id in seen:
            continue
        seen.add(prediction.prediction_id)
        prediction_rows_list.append(serialize_prediction_row(prediction, provenance=provenance))
    fold_rows: list[dict[str, JsonValue]] = []
    for fold in result.folds:
        aggregate = next(
            (
                item
                for item in result.metrics.aggregations
                if item.dimension == "fold" and item.key == fold.fold_id
            ),
            None,
        )
        fold_rows.append(
            serialize_fold_metrics_row(
                fold_id=fold.fold_id,
                sample_size=0 if aggregate is None else aggregate.sample_size,
                accepted_single_count=0 if aggregate is None else aggregate.accepted_single_count,
                accepted_combination_count=(
                    0 if aggregate is None else aggregate.accepted_combination_count
                ),
                net_profit_units=Decimal("0") if aggregate is None else aggregate.net_profit_units,
            )
        )
    return {
        "predictions": tuple(prediction_rows_list),
        "market_evaluations": tuple(evaluation_rows),
        "opportunities": opportunity_rows,
        "opportunity_decisions": tuple(
            serialize_opportunity_decision_row(item) for item in result.opportunity_decisions
        ),
        "combinations": tuple(serialize_combination_row(item) for item in result.combinations),
        "rejections": tuple(
            serialize_opportunity_filter_rejection_row(
                item,
                filter_config_id=next(
                    (decision.filter_config_id for decision in result.opportunity_decisions),
                    "unknown-filter",
                ),
                strategy_id=result.strategy_id,
            )
            for item in result.opportunity_rejections
        )
        + tuple(
            serialize_combination_builder_rejection_row(
                item,
                policy_id=result.strategy_id,
                truncated=False,
            )
            for item in result.combination_rejections
        ),
        "settlements": tuple(
            serialize_settlement_row(
                bet,
                strategy_id=result.strategy_id,
                combination_id=bet.combination_id or None,
            )
            for bet in result.bets
        ),
        "fold_metrics": tuple(fold_rows),
        "aggregate_metrics": (
            serialize_aggregate_metrics_row(
                backtest_id=result.backtest_result_id,
                result=result,
                feature_artifact_id=feature_artifact_id,
                feature_manifest_checksum_sha256=feature_manifest_checksum_sha256,
                input_snapshots=input_snapshots,
                random_seed=random_seed,
                test_event_count=test_event_count,
                complete_quote_event_count=complete_quote_event_count,
                quote_coverage=quote_coverage,
            ),
        ),
    }


def _prediction_from_opportunity(opportunity: Opportunity, *, provenance: str) -> MarketPrediction:
    from sports_analytics.predictions.contracts import (
        PredictionLineage,
        PredictionQualityFlags,
        SelectionProbability,
        build_market_prediction,
    )

    lineage = PredictionLineage(
        model_artifact_id=opportunity.model_artifact_id,
        model_checksum_sha256=opportunity.model_checksum_sha256,
        model_specification_version=opportunity.model_specification_version,
        feature_artifact_id=opportunity.feature_artifact_id,
        feature_manifest_checksum_sha256=opportunity.feature_manifest_checksum_sha256,
        feature_specification_version=opportunity.feature_specification_version,
        feature_row_id=opportunity.feature_row_id,
        trained_through_date=opportunity.model_trained_through_date,
        calibrated_through_date=opportunity.model_calibrated_through_date,
    )
    return build_market_prediction(
        canonical_event_id=opportunity.canonical_event_id,
        event_start_utc=opportunity.event_start_utc,
        predicted_at_utc=opportunity.predicted_at_utc,
        feature_available_at_utc=opportunity.predicted_at_utc,
        lineage=lineage,
        probabilities=(SelectionProbability(opportunity.selection, opportunity.model_probability),),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=False,
        ),
    )


def _combination_rejection_code(reason: str) -> str:
    lowered = reason.casefold()
    if "unknown dependency" in lowered:
        return "unknown-dependency"
    if "conflicting legs" in lowered:
        return "combination-conflict"
    if "timing" in lowered:
        return "timing"
    if "odds" in lowered or "rules" in lowered:
        return "odds-rules"
    if "truncat" in lowered:
        return "builder-truncation"
    return "combination-rejected"

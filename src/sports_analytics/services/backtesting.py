"""Football rolling-origin benchmark orchestration and artifact publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from sports_analytics.artifacts import (
    TypedAnalyticalArtifact,
    write_typed_analytical_artifact,
)
from sports_analytics.backtesting.contracts import BacktestResult, SettledOpportunity
from sports_analytics.backtesting.football import (
    FootballClosingBenchmark,
    run_football_1x2_closing_benchmark,
)
from sports_analytics.core.exceptions import BacktestError, FeatureError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.features.football.datasets import load_feature_artifact
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.opportunities.contracts import Opportunity, OpportunityFilter

FOOTBALL_CLOSING_BACKTEST_SCHEMA: str = "football-1x2-closing-backtest-v1"


@dataclass(frozen=True, slots=True)
class FootballBacktestRequest:
    """Fixed strategy inputs for a football closing-market benchmark."""

    feature_relative_directory: str
    feature_manifest_checksum: str | None = None
    minimum_probability: float = 0.0
    minimum_edge: float = 0.0
    minimum_expected_value: float = 0.0
    selection_minimum_odds: Decimal = Decimal("1.0001")
    selection_maximum_odds: Decimal = Decimal("100000")
    random_seed: int = 42


@dataclass(frozen=True, slots=True)
class PublishedFootballBacktest:
    """In-memory benchmark plus verified immutable artifact."""

    benchmark: FootballClosingBenchmark
    artifact: TypedAnalyticalArtifact


def run_and_publish_football_closing_backtest(
    *,
    paths: RuntimePaths,
    request: FootballBacktestRequest,
) -> PublishedFootballBacktest:
    """Load a verified feature artifact, run folds, and atomically publish results."""
    try:
        manifest, vectors, quotes, folds = load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=request.feature_relative_directory,
            expected_manifest_checksum=request.feature_manifest_checksum,
        )
    except FeatureError as exc:
        raise BacktestError(str(exc)) from exc
    checksum_path = (
        paths.features_directory
        / request.feature_relative_directory.replace("\\", "/")
        / "manifest_checksum.sha256"
    )
    manifest_checksum = checksum_path.read_text(encoding="utf-8").strip()
    filters = OpportunityFilter(
        minimum_probability=request.minimum_probability,
        minimum_edge=request.minimum_edge,
        minimum_expected_value=request.minimum_expected_value,
        selection_minimum_odds=request.selection_minimum_odds,
        selection_maximum_odds=request.selection_maximum_odds,
        sport_codes=frozenset({"football"}),
        market_keys=frozenset({"football.match-result.1x2.full-match"}),
        provider_ids=frozenset({"market-average"}),
        include_historical_benchmarks=True,
    )
    benchmark = run_football_1x2_closing_benchmark(
        vectors=vectors,
        quotes=quotes,
        folds=folds,
        feature_artifact_id=str(manifest["artifact_id"]),
        feature_manifest_checksum_sha256=manifest_checksum,
        filters=filters,
        random_seed=request.random_seed,
    )
    datasets = _benchmark_datasets(
        benchmark,
        feature_artifact_id=str(manifest["artifact_id"]),
        feature_manifest_checksum=manifest_checksum,
        request=request,
    )
    competition_id = str(manifest["competition_id"])
    relative = (
        f"backtests/{FOOTBALL_CLOSING_BACKTEST_SCHEMA}/"
        f"{competition_id}/{benchmark.result.backtest_id}"
    )
    artifact = write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        artifact_kind="backtest",
        schema_version=FOOTBALL_CLOSING_BACKTEST_SCHEMA,
        datasets=datasets,
    )
    return PublishedFootballBacktest(benchmark=benchmark, artifact=artifact)


def _benchmark_datasets(
    benchmark: FootballClosingBenchmark,
    *,
    feature_artifact_id: str,
    feature_manifest_checksum: str,
    request: FootballBacktestRequest,
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    result = benchmark.result
    metrics = result.metrics
    candidates_by_prediction: dict[str, list[SettledOpportunity]] = {}
    for settled in result.candidates:
        candidates_by_prediction.setdefault(
            settled.opportunity.prediction_id,
            [],
        ).append(settled)
    prediction_rows: list[dict[str, JsonValue]] = []
    evaluation_rows: list[dict[str, JsonValue]] = []
    opportunity_rows: list[dict[str, JsonValue]] = []
    for prediction_id, settled_rows in candidates_by_prediction.items():
        opportunities = [item.opportunity for item in settled_rows]
        first = opportunities[0]
        prediction_rows.append(
            {
                "prediction_id": prediction_id,
                "canonical_event_id": first.canonical_event_id,
                "event_start_utc": format_utc_timestamp(first.event_start_utc),
                "predicted_at_utc": format_utc_timestamp(first.predicted_at_utc),
                "ordered_selection_ids": [item.selection.selection_id for item in opportunities],
                "probabilities": [
                    {
                        "selection_id": item.selection.selection_id,
                        "probability": item.model_probability,
                    }
                    for item in opportunities
                ],
                "model_artifact_id": first.model_artifact_id,
                "model_checksum_sha256": first.model_checksum_sha256,
                "model_specification_version": first.model_specification_version,
                "feature_artifact_id": first.feature_artifact_id,
                "feature_manifest_checksum_sha256": (first.feature_manifest_checksum_sha256),
                "feature_specification_version": first.feature_specification_version,
                "feature_row_id": first.feature_row_id,
                "calibrated": True,
                "artifact_verified": True,
                "sufficient_history": True,
                "data_quality_passed": True,
            }
        )
        for opportunity in opportunities:
            evaluation_rows.append(
                {
                    "evaluation_id": opportunity.opportunity_id,
                    "prediction_id": prediction_id,
                    "opportunity_id": opportunity.opportunity_id,
                    "selection_id": opportunity.selection.selection_id,
                    "provider_id": opportunity.provider_id,
                    "decimal_odds": format(opportunity.decimal_odds, "f"),
                    "model_probability": opportunity.model_probability,
                    "raw_implied_probability": opportunity.raw_implied_probability,
                    "normalized_implied_probability": (opportunity.normalized_implied_probability),
                    "overround": opportunity.overround,
                    "edge": opportunity.edge,
                    "expected_value": opportunity.expected_value,
                }
            )
            opportunity_rows.append(_opportunity_row(opportunity))
    decision_rows = cast(
        tuple[dict[str, JsonValue], ...],
        tuple(
            {
                "opportunity_id": item.opportunity_id,
                "filter_config_id": item.filter_config_id,
                "decision_as_of_utc": format_utc_timestamp(item.decision_as_of_utc),
                "eligible": item.eligible,
                "rejection_codes": [code.value for code in item.rejection_codes],
                "accepted_rank": item.accepted_rank,
            }
            for item in result.opportunity_decisions
        ),
    )
    rejection_rows = cast(
        tuple[dict[str, JsonValue], ...],
        tuple(
            {
                "rejection_id": content_addressed_id(
                    identity_type="backtest-opportunity-rejection-v1",
                    payload={
                        "opportunity_id": item.opportunity.opportunity_id,
                        "codes": [code.value for code in item.codes],
                        "strategy_id": result.strategy_id,
                    },
                ),
                "opportunity_id": item.opportunity.opportunity_id,
                "codes": [code.value for code in item.codes],
            }
            for item in result.opportunity_rejections
        ),
    )
    settlement_rows = cast(
        tuple[dict[str, JsonValue], ...],
        tuple(
            {
                "bet_id": bet.bet_id,
                "fold_id": bet.fold_id,
                "kind": bet.kind.value,
                "opportunity_ids": list(bet.opportunity_ids),
                "event_start_utc": (
                    None
                    if bet.event_start_utc is None
                    else format_utc_timestamp(bet.event_start_utc)
                ),
                "decimal_odds": format(bet.decimal_odds, "f"),
                "result": bet.result.value,
                "stake_units": format(bet.stake_units, "f"),
                "profit_units": format(bet.profit_units, "f"),
            }
            for bet in result.bets
        ),
    )
    fold_rows = tuple(_fold_metric_row(fold.fold_id, result) for fold in result.folds)
    summary: dict[str, JsonValue] = {
        "metric_id": "aggregate",
        "backtest_id": result.backtest_id,
        "mode": result.mode.value,
        "strategy_id": result.strategy_id,
        "feature_artifact_id": feature_artifact_id,
        "feature_manifest_checksum_sha256": feature_manifest_checksum,
        "random_seed": request.random_seed,
        "test_event_count": benchmark.test_event_count,
        "complete_quote_event_count": benchmark.complete_quote_event_count,
        "quote_coverage": benchmark.quote_coverage,
        "candidate_count": metrics.candidate_count,
        "rejection_count": metrics.rejection_count,
        "accepted_single_count": metrics.accepted_single_count,
        "accepted_combination_count": metrics.accepted_combination_count,
        "gross_return_units": format(metrics.gross_return_units, "f"),
        "net_profit_units": format(metrics.net_profit_units, "f"),
        "roi": metrics.roi,
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
        "disclaimer": result.disclaimer,
    }
    return {
        "predictions": tuple(prediction_rows),
        "market_evaluations": tuple(evaluation_rows),
        "opportunity_decisions": decision_rows,
        "opportunities": tuple(opportunity_rows),
        "combinations": (),
        "rejections": rejection_rows,
        "settlements": settlement_rows,
        "fold_metrics": fold_rows,
        "aggregate_metrics": (summary,),
    }


def _opportunity_row(opportunity: Opportunity) -> dict[str, JsonValue]:
    return {
        "opportunity_id": opportunity.opportunity_id,
        "canonical_event_id": opportunity.canonical_event_id,
        "event_start_utc": format_utc_timestamp(opportunity.event_start_utc),
        "selection_id": opportunity.selection.selection_id,
        "sport_code": opportunity.selection.sport_code,
        "market_key": opportunity.selection.market_key,
        "prediction_id": opportunity.prediction_id,
        "decision_as_of_utc": format_utc_timestamp(cast(datetime, opportunity.decision_as_of_utc)),
        "quote_observation_id": opportunity.quote_observation_id,
        "provider_id": opportunity.provider_id,
        "decimal_odds": format(opportunity.decimal_odds, "f"),
        "model_probability": opportunity.model_probability,
        "edge": opportunity.edge,
        "expected_value": opportunity.expected_value,
        "model_artifact_id": opportunity.model_artifact_id,
        "model_checksum_sha256": opportunity.model_checksum_sha256,
        "model_specification_version": opportunity.model_specification_version,
        "feature_artifact_id": opportunity.feature_artifact_id,
        "feature_manifest_checksum_sha256": (opportunity.feature_manifest_checksum_sha256),
        "feature_specification_version": opportunity.feature_specification_version,
        "feature_row_id": opportunity.feature_row_id,
    }


def _fold_metric_row(fold_id: str, result: BacktestResult) -> dict[str, JsonValue]:
    aggregate = next(
        (
            item
            for item in result.metrics.aggregations
            if item.dimension == "fold" and item.key == fold_id
        ),
        None,
    )
    return {
        "fold_id": fold_id,
        "sample_size": 0 if aggregate is None else aggregate.sample_size,
        "accepted_single_count": (0 if aggregate is None else aggregate.accepted_single_count),
        "accepted_combination_count": (
            0 if aggregate is None else aggregate.accepted_combination_count
        ),
        "net_profit_units": ("0" if aggregate is None else format(aggregate.net_profit_units, "f")),
    }

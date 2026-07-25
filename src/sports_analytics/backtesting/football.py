"""Football 1X2 rolling-origin closing-line historical benchmark."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from sports_analytics.backtesting.contracts import (
    BacktestFold,
    BacktestLineage,
    BacktestMode,
    BacktestResult,
    FoldBacktestInput,
    SettledOpportunity,
    SettlementResult,
    StrategyConfiguration,
)
from sports_analytics.backtesting.engine import run_backtest
from sports_analytics.core.exceptions import BacktestError, ValueEvaluationError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.types import JsonValue
from sports_analytics.evaluation.temporal import TemporalFold
from sports_analytics.features.football.datasets import ClosingMarketQuoteTriple
from sports_analytics.features.football.prematch import FeatureVector
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.models.calibration import fit_temperature, softmax
from sports_analytics.models.football_1x2 import (
    FOOTBALL_1X2_LOGISTIC_MODEL_V1,
    matrix_from_vectors,
    select_vectors,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.models.logistic import (
    LogisticConfiguration,
    fit_multinomial_logistic,
    logits_from_parameters,
)
from sports_analytics.opportunities.contracts import (
    OpportunityFilter,
    opportunities_from_evaluation,
)
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    MarketPrediction,
    PredictionInputSnapshot,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.predictions.replay import derive_historical_replay_cutoff_utc
from sports_analytics.sports.football.markets import match_result_1x2_selection
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    MarketValueEvaluation,
    PricedSelection,
    QuoteEvaluationMode,
    evaluate_complete_market,
)


@dataclass(frozen=True, slots=True)
class FootballClosingBenchmark:
    """Result plus explicit complete-quote coverage."""

    result: BacktestResult
    test_event_count: int
    complete_quote_event_count: int
    quote_coverage: float
    predictions: tuple[MarketPrediction, ...] = ()
    evaluations: tuple[MarketValueEvaluation, ...] = ()


def run_football_1x2_closing_benchmark(
    *,
    vectors: tuple[FeatureVector, ...],
    quotes: tuple[ClosingMarketQuoteTriple, ...],
    folds: tuple[TemporalFold, ...],
    feature_artifact_id: str,
    feature_manifest_checksum_sha256: str,
    filters: OpportunityFilter,
    random_seed: int,
    input_snapshots: tuple[PredictionInputSnapshot, ...] = (),
) -> FootballClosingBenchmark:
    """Fit each fold in-origin and evaluate singles against closing market average."""
    quote_by_event = {item.canonical_event_id: item for item in quotes}
    if len(quote_by_event) != len(quotes):
        raise BacktestError("closing quote input contains duplicate event ids")
    fold_inputs: list[FoldBacktestInput] = []
    test_events = 0
    quoted_events = 0
    all_predictions: list[MarketPrediction] = []
    all_evaluations: list[MarketValueEvaluation] = []
    config = LogisticConfiguration(random_seed=random_seed)
    for fold in folds:
        train = select_vectors(vectors, fold.train.event_ids)
        calibration = select_vectors(vectors, fold.calibration.event_ids)
        test = select_vectors(vectors, fold.test.event_ids)
        test_events += len(test)
        labels = tuple(item.result_code for item in train)
        if set(labels) != set(FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels):
            raise BacktestError(f"{fold.fold_id} training region lacks a complete outcome space")
        parameters = fit_multinomial_logistic(
            feature_matrix=matrix_from_vectors(train),
            labels=labels,
            feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
            outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
            configuration=config,
        )
        calibration_logits = logits_from_parameters(
            feature_vector=matrix_from_vectors(calibration),
            parameters=parameters,
        )
        temperature = fit_temperature(
            logits=calibration_logits,
            labels=tuple(item.result_code for item in calibration),
            outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        ).temperature
        test_probabilities = softmax(
            logits_from_parameters(
                feature_vector=matrix_from_vectors(test),
                parameters=parameters,
            ),
            outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
            temperature=temperature,
        )
        parameter_payload: dict[str, JsonValue] = {
            "fold_id": fold.fold_id,
            "model_specification_version": FOOTBALL_1X2_LOGISTIC_MODEL_V1,
            "feature_specification_version": FOOTBALL_1X2_PREMATCH_FEATURES_V1,
            "feature_names": list(parameters.feature_names),
            "outcome_labels": list(parameters.outcome_labels),
            "logistic_configuration": {
                "configuration_version": parameters.configuration.configuration_version,
                "solver": parameters.configuration.solver,
                "penalty": parameters.configuration.penalty,
                "regularization_strength": parameters.configuration.regularization_strength,
                "tolerance": parameters.configuration.tolerance,
                "maximum_iterations": parameters.configuration.maximum_iterations,
                "fit_intercept": parameters.configuration.fit_intercept,
                "random_seed": parameters.configuration.random_seed,
                "feature_scaler_policy": parameters.configuration.feature_scaler_policy,
            },
            "sklearn_version": parameters.sklearn_version,
            "numpy_version": parameters.numpy_version,
            "coefficients": [list(item) for item in parameters.coefficients],
            "intercepts": list(parameters.intercepts),
            "scaler_mean": list(parameters.scaler_mean),
            "scaler_scale": list(parameters.scaler_scale),
            "temperature": temperature,
            "random_seed": random_seed,
        }
        model_id = content_addressed_id(
            identity_type="football-1x2-backtest-fold-model-v1",
            payload=parameter_payload,
        )
        model_checksum = hashlib.sha256(
            dumps_canonical_json(parameter_payload).encode("utf-8")
        ).hexdigest()
        settled: list[SettledOpportunity] = []
        for vector, probabilities in zip(test, test_probabilities, strict=True):
            quote = quote_by_event.get(vector.metadata.canonical_event_id)
            if quote is None:
                continue
            start = vector.metadata.scheduled_start_utc
            if start is None:
                continue
            quoted_events += 1
            replay_cutoff = derive_historical_replay_cutoff_utc(start)
            lineage = PredictionLineage(
                model_artifact_id=model_id,
                model_checksum_sha256=model_checksum,
                model_specification_version=FOOTBALL_1X2_LOGISTIC_MODEL_V1,
                feature_artifact_id=feature_artifact_id,
                feature_manifest_checksum_sha256=feature_manifest_checksum_sha256,
                feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
                feature_row_id=vector.metadata.canonical_event_id,
                trained_through_date=fold.train.end_date,
                calibrated_through_date=fold.calibration.end_date,
                input_snapshots=input_snapshots,
            )
            selection_probabilities = tuple(
                SelectionProbability(
                    selection=CanonicalSelectionIdentity.from_selection(
                        match_result_1x2_selection(label)
                    ),
                    probability=float(probabilities[index]),
                )
                for index, label in enumerate(FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels)
            )
            prediction = build_market_prediction(
                canonical_event_id=vector.metadata.canonical_event_id,
                event_start_utc=start,
                predicted_at_utc=replay_cutoff,
                feature_available_at_utc=replay_cutoff,
                lineage=lineage,
                probabilities=selection_probabilities,
                quality=PredictionQualityFlags(
                    calibrated=True,
                    model_artifact_verified=True,
                    feature_artifact_verified=bool(input_snapshots),
                    sufficient_history=True,
                    data_quality_passed=False,
                ),
                provenance=PredictionProvenance.HISTORICAL_REPLAY,
            )
            complete_quote = _complete_quote(quote)
            try:
                evaluation = evaluate_complete_market(
                    prediction=prediction,
                    quote=complete_quote,
                    mode=QuoteEvaluationMode.CLOSING_LINE_HISTORICAL_BENCHMARK,
                )
            except ValueEvaluationError:
                continue
            all_predictions.append(prediction)
            all_evaluations.append(evaluation)
            for opportunity in opportunities_from_evaluation(evaluation):
                settled.append(
                    SettledOpportunity(
                        opportunity=opportunity,
                        result=(
                            SettlementResult.WIN
                            if opportunity.selection.outcome_key == vector.result_code
                            else SettlementResult.LOSS
                        ),
                    )
                )
        fold_inputs.append(
            FoldBacktestInput(
                fold=BacktestFold(
                    fold_id=fold.fold_id,
                    train_start_date=fold.train.start_date,
                    train_end_date=fold.train.end_date,
                    calibration_start_date=fold.calibration.start_date,
                    calibration_end_date=fold.calibration.end_date,
                    test_start_date=fold.test.start_date,
                    test_end_date=fold.test.end_date,
                ),
                candidates=tuple(settled),
                fold_model_id=model_id,
                fold_model_checksum_sha256=model_checksum,
                fold_model_payload=parameter_payload,
                calibration_temperature=temperature,
                random_seed=random_seed,
            )
        )
    if quoted_events == 0:
        raise BacktestError(
            "football closing benchmark found no test events with complete quotes "
            "and precise scheduled starts"
        )
    strategy = StrategyConfiguration(
        strategy_version="football-1x2-closing-market-average-singles-v1",
        mode=BacktestMode.CLOSING_LINE_HISTORICAL_BENCHMARK,
        opportunity_filter=filters,
        include_singles=True,
        include_combinations=False,
    )
    result = run_backtest(
        tuple(fold_inputs),
        strategy=strategy,
        lineage=BacktestLineage(
            feature_artifact_id=feature_artifact_id,
            feature_manifest_checksum_sha256=feature_manifest_checksum_sha256,
            input_snapshots=tuple(
                {
                    "snapshot_id": item.snapshot_id,
                    "manifest_checksum_sha256": item.manifest_checksum_sha256,
                    "schema_version": item.schema_version,
                    "source_name": item.source_name,
                }
                for item in input_snapshots
            ),
        ),
    )
    return FootballClosingBenchmark(
        result=result,
        test_event_count=test_events,
        complete_quote_event_count=quoted_events,
        quote_coverage=quoted_events / test_events if test_events else 0.0,
        predictions=tuple(all_predictions),
        evaluations=tuple(all_evaluations),
    )


def _complete_quote(item: ClosingMarketQuoteTriple) -> CompleteMarketQuote:
    identity = {
        label: CanonicalSelectionIdentity.from_selection(match_result_1x2_selection(label))
        for label in FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels
    }
    values = {
        "home": (
            item.home_odds,
            item.home_quote_series_id,
            item.home_quote_observation_id,
        ),
        "draw": (
            item.draw_odds,
            item.draw_quote_series_id,
            item.draw_quote_observation_id,
        ),
        "away": (
            item.away_odds,
            item.away_quote_series_id,
            item.away_quote_observation_id,
        ),
    }
    priced = tuple(
        PricedSelection(
            selection=identity[label],
            decimal_odds=Decimal(str(values[label][0])),
            quote_series_id=str(values[label][1]),
            quote_observation_id=str(values[label][2]),
        )
        for label in FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels
    )
    return CompleteMarketQuote(
        canonical_event_id=item.canonical_event_id,
        source_name=item.source_name,
        provider_type=item.provider_type,
        provider_id=item.provider_id,
        quote_phase=item.quote_phase,
        source_observed_at_utc=item.source_observed_at_utc,
        quoted_at_utc=item.quoted_at_utc,
        quote_timestamp_precision=item.quote_timestamp_precision,
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=priced,
    )

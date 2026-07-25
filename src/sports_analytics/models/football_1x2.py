"""Football full-match 1X2 baseline training, calibration, and evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from sports_analytics.core.exceptions import ModelError, TrainingError
from sports_analytics.data.codec import ensure_json_value
from sports_analytics.data.types import JsonValue
from sports_analytics.evaluation.metrics import (
    closing_odds_to_normalized_probabilities,
    evaluate_probabilities,
    metrics_to_json,
)
from sports_analytics.evaluation.temporal import TemporalFold, TemporalSplitConfig
from sports_analytics.features.football.datasets import ClosingMarketQuoteTriple
from sports_analytics.features.football.prematch import FeatureVector
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.models.artifacts import (
    FeatureArtifactLineage,
    ModelArtifact,
    build_model_document,
    infer_calibrated_probabilities,
    load_model_artifact,
    write_model_artifact,
)
from sports_analytics.models.calibration import fit_temperature, softmax
from sports_analytics.models.contracts import ModelSpecification
from sports_analytics.models.identity import content_addressed_id, validate_artifact_id_override
from sports_analytics.models.logistic import (
    LogisticConfiguration,
    fit_multinomial_logistic,
    logits_from_parameters,
)

FOOTBALL_1X2_LOGISTIC_MODEL_V1: str = "football-1x2-logistic-v1"
OUTCOME_LABELS_1X2: tuple[str, ...] = FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels


def football_1x2_logistic_specification(
    feature_specification_version: str,
) -> ModelSpecification:
    """Return the football 1X2 logistic model specification."""
    if feature_specification_version != FOOTBALL_1X2_PREMATCH_FEATURES_V1:
        msg = (
            "unsupported feature specification version for football 1X2 logistic model: "
            f"{feature_specification_version}"
        )
        raise ModelError(msg)
    return ModelSpecification(
        model_specification_version=FOOTBALL_1X2_LOGISTIC_MODEL_V1,
        sport_code="football",
        market_key="football.match-result.1x2.full-match",
        algorithm="multinomial-logistic-regression",
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        feature_specification_version=feature_specification_version,
        description="Team-level multinomial logistic baseline for football full-match 1X2.",
    )


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    """Metrics and metadata recorded for one temporal fold."""

    fold_id: str
    train_date_range: tuple[str, str]
    calibration_date_range: tuple[str, str]
    test_date_range: tuple[str, str]
    event_counts: dict[str, int]
    class_counts: dict[str, dict[str, int]]
    temperature: float
    uncalibrated_metrics: dict[str, object]
    calibrated_metrics: dict[str, object]
    market_benchmark_metrics: dict[str, object] | None
    market_benchmark_coverage: float
    calibration_improved_log_loss: bool
    random_seed: int
    feature_specification_version: str
    training_configuration: dict[str, object]


@dataclass(frozen=True, slots=True)
class Football1x2TrainingResult:
    """Complete training run result including the final deployable artifact."""

    folds: tuple[FoldEvaluation, ...]
    final_artifact_relative_directory: str
    final_artifact_checksum: str
    final_artifact: ModelArtifact
    trained_through_date: date
    calibrated_through_date: date


def matrix_from_vectors(vectors: tuple[FeatureVector, ...] | list[FeatureVector]) -> np.ndarray:
    """Stack ordered feature values into a dense float matrix."""
    if not vectors:
        msg = "cannot build feature matrix from empty vectors"
        raise ModelError(msg)
    return np.asarray([item.ordered_values() for item in vectors], dtype=np.float64)


def select_vectors(
    vectors: tuple[FeatureVector, ...],
    event_ids: tuple[str, ...],
) -> tuple[FeatureVector, ...]:
    """Select feature rows by canonical event id, preserving fold order."""
    by_id = {item.metadata.canonical_event_id: item for item in vectors}
    try:
        return tuple(by_id[event_id] for event_id in event_ids)
    except KeyError as exc:
        msg = f"fold references unknown event id: {exc}"
        raise TrainingError(msg) from exc


def evaluate_fold(
    *,
    fold: TemporalFold,
    vectors: tuple[FeatureVector, ...],
    closing_quotes: tuple[ClosingMarketQuoteTriple, ...],
    random_seed: int,
    logistic_configuration: LogisticConfiguration | None = None,
) -> FoldEvaluation:
    """Fit, calibrate, and evaluate one untouched test region."""
    config = logistic_configuration or LogisticConfiguration(random_seed=random_seed)
    train = select_vectors(vectors, fold.train.event_ids)
    calibration = select_vectors(vectors, fold.calibration.event_ids)
    test = select_vectors(vectors, fold.test.event_ids)

    train_labels = tuple(item.result_code for item in train)
    if set(train_labels) != set(OUTCOME_LABELS_1X2):
        msg = f"fold {fold.fold_id} training region does not contain all three outcomes"
        raise TrainingError(msg)

    parameters = fit_multinomial_logistic(
        feature_matrix=matrix_from_vectors(train),
        labels=train_labels,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=config,
    )
    cal_logits = logits_from_parameters(
        feature_vector=matrix_from_vectors(calibration),
        parameters=parameters,
    )
    temperature_result = fit_temperature(
        logits=cal_logits,
        labels=tuple(item.result_code for item in calibration),
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    test_logits = logits_from_parameters(
        feature_vector=matrix_from_vectors(test),
        parameters=parameters,
    )
    test_labels = tuple(item.result_code for item in test)
    uncalibrated = softmax(
        test_logits,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        temperature=1.0,
    )
    calibrated = softmax(
        test_logits,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        temperature=temperature_result.temperature,
    )
    uncalibrated_metrics = evaluate_probabilities(
        labels=test_labels,
        probabilities=uncalibrated,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    calibrated_metrics = evaluate_probabilities(
        labels=test_labels,
        probabilities=calibrated,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )

    benchmark_metrics, coverage = _market_benchmark(
        test=test,
        closing_quotes=closing_quotes,
    )
    return FoldEvaluation(
        fold_id=fold.fold_id,
        train_date_range=(fold.train.start_date.isoformat(), fold.train.end_date.isoformat()),
        calibration_date_range=(
            fold.calibration.start_date.isoformat(),
            fold.calibration.end_date.isoformat(),
        ),
        test_date_range=(fold.test.start_date.isoformat(), fold.test.end_date.isoformat()),
        event_counts={
            "train": len(train),
            "calibration": len(calibration),
            "test": len(test),
        },
        class_counts={
            "train": dict(fold.train.class_counts),
            "calibration": dict(fold.calibration.class_counts),
            "test": dict(fold.test.class_counts),
        },
        temperature=temperature_result.temperature,
        uncalibrated_metrics=metrics_to_json(uncalibrated_metrics),
        calibrated_metrics=metrics_to_json(calibrated_metrics),
        market_benchmark_metrics=benchmark_metrics,
        market_benchmark_coverage=coverage,
        calibration_improved_log_loss=(calibrated_metrics.log_loss < uncalibrated_metrics.log_loss),
        random_seed=random_seed,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        training_configuration={
            "algorithm": "multinomial-logistic-regression",
            "calibration": "temperature-scaling",
            "logistic_configuration": {
                "configuration_version": config.configuration_version,
                "solver": config.solver,
                "penalty": config.penalty,
                "regularization_strength": config.regularization_strength,
                "tolerance": config.tolerance,
                "maximum_iterations": config.maximum_iterations,
                "fit_intercept": config.fit_intercept,
                "feature_scaler_policy": config.feature_scaler_policy,
            },
        },
    )


def train_final_artifact(
    *,
    vectors: tuple[FeatureVector, ...],
    folds: tuple[TemporalFold, ...],
    closing_quotes: tuple[ClosingMarketQuoteTriple, ...],
    feature_lineage: FeatureArtifactLineage,
    competition_id: str,
    models_root: Any,
    random_seed: int,
    split_config: TemporalSplitConfig,
    artifact_id: str | None = None,
    logistic_configuration: LogisticConfiguration | None = None,
) -> Football1x2TrainingResult:
    """Run rolling-origin evaluation and persist one final deployable artifact."""
    if not folds:
        msg = "training requires at least one valid temporal fold"
        raise TrainingError(msg)

    config = logistic_configuration or LogisticConfiguration(random_seed=random_seed)
    fold_evaluations = tuple(
        evaluate_fold(
            fold=fold,
            vectors=vectors,
            closing_quotes=closing_quotes,
            random_seed=random_seed,
            logistic_configuration=config,
        )
        for fold in folds
    )
    final_fold = folds[-1]
    train = select_vectors(vectors, final_fold.train.event_ids)
    history = tuple(
        item for item in vectors if item.metadata.event_date < final_fold.calibration.start_date
    )
    calibration = select_vectors(vectors, final_fold.calibration.event_ids)
    if len(history) < split_config.min_train_rows:
        history = train
    history_labels = tuple(item.result_code for item in history)
    if set(history_labels) != set(OUTCOME_LABELS_1X2):
        msg = "final training history must contain all three outcomes"
        raise TrainingError(msg)

    parameters = fit_multinomial_logistic(
        feature_matrix=matrix_from_vectors(history),
        labels=history_labels,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=config,
    )
    cal_logits = logits_from_parameters(
        feature_vector=matrix_from_vectors(calibration),
        parameters=parameters,
    )
    temperature_result = fit_temperature(
        logits=cal_logits,
        labels=tuple(item.result_code for item in calibration),
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )

    calibrated_through = final_fold.calibration.end_date
    if any(item.metadata.event_date > calibrated_through for item in calibration):
        msg = "calibration window exceeds calibrated_through_date"
        raise TrainingError(msg)

    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    identity_payload: dict[str, JsonValue] = {
        "feature_artifact_id": feature_lineage.feature_artifact_id,
        "feature_manifest_checksum_sha256": feature_lineage.feature_manifest_checksum_sha256,
        "folds_file_checksum_sha256": feature_lineage.folds_file_checksum_sha256,
        "model_specification_version": specification.model_specification_version,
        "feature_specification_version": specification.feature_specification_version,
        "fold_configuration": feature_lineage.fold_configuration,
        "random_seed": random_seed,
        "logistic_configuration": {
            "configuration_version": config.configuration_version,
            "solver": config.solver,
            "penalty": config.penalty,
            "regularization_strength": config.regularization_strength,
            "tolerance": config.tolerance,
            "maximum_iterations": config.maximum_iterations,
            "fit_intercept": config.fit_intercept,
            "feature_scaler_policy": config.feature_scaler_policy,
        },
    }
    derived_id = content_addressed_id(
        identity_type="football-1x2-model-artifact",
        payload=identity_payload,
    )
    resolved_id = validate_artifact_id_override(
        override=artifact_id,
        derived=derived_id,
        artifact_kind="model",
    )
    relative_directory = (
        f"football/{specification.model_specification_version}/{competition_id}/{resolved_id}"
    )
    validation_metrics: dict[str, JsonValue] = {
        "folds": [_fold_evaluation_json(item) for item in fold_evaluations],
    }
    evaluation_summary: dict[str, JsonValue] = {
        "fold_count": len(fold_evaluations),
        "final_fold_id": final_fold.fold_id,
        "mean_calibrated_log_loss": float(
            np.mean([float(str(item.calibrated_metrics["log_loss"])) for item in fold_evaluations])
        ),
        "mean_calibrated_brier_score": float(
            np.mean(
                [float(str(item.calibrated_metrics["brier_score"])) for item in fold_evaluations]
            )
        ),
        "calibration_improved_fold_count": sum(
            1 for item in fold_evaluations if item.calibration_improved_log_loss
        ),
    }
    trained_through_date = max(item.metadata.event_date for item in history)
    document = build_model_document(
        artifact_id=resolved_id,
        specification=specification,
        parameters=parameters,
        temperature=temperature_result.temperature,
        competition_id=competition_id,
        trained_through_date=trained_through_date,
        calibrated_through_date=calibrated_through,
        feature_lineage=feature_lineage,
        configuration={
            "temporal_split": ensure_json_value(split_config.to_json()),
            "random_seed": random_seed,
            "feature_specification_version": FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        },
        validation_metrics=validation_metrics,
        evaluation_summary=evaluation_summary,
        random_seed=random_seed,
    )
    _path, checksum = write_model_artifact(
        models_root=models_root,
        relative_directory=relative_directory,
        document=document,
    )
    artifact = load_model_artifact(
        models_root=models_root,
        relative_path=f"{relative_directory}/model.json",
        specification=specification,
        expected_checksum=checksum,
    )
    sample = history[0]
    persisted = infer_calibrated_probabilities(
        artifact=artifact,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        feature_values=sample.ordered_values(),
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
    )
    direct_logits = logits_from_parameters(
        feature_vector=np.asarray(sample.ordered_values(), dtype=np.float64),
        parameters=parameters,
    )
    direct = softmax(
        direct_logits,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        temperature=temperature_result.temperature,
    )[0]
    for index, label in enumerate(OUTCOME_LABELS_1X2):
        if abs(persisted[label] - float(direct[index])) > 1e-12:
            msg = "persisted inference diverged from training-time inference"
            raise TrainingError(msg)

    return Football1x2TrainingResult(
        folds=fold_evaluations,
        final_artifact_relative_directory=relative_directory,
        final_artifact_checksum=checksum,
        final_artifact=artifact,
        trained_through_date=artifact.trained_through_date,
        calibrated_through_date=artifact.calibrated_through_date,
    )


def prepare_folds(
    vectors: tuple[FeatureVector, ...],
    *,
    config: TemporalSplitConfig | None = None,
) -> tuple[TemporalFold, ...]:
    """Build chronological folds for a feature dataset."""
    from sports_analytics.evaluation.temporal import build_rolling_origin_folds

    return build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        config=config,
    )


def _market_benchmark(
    *,
    test: tuple[FeatureVector, ...],
    closing_quotes: tuple[ClosingMarketQuoteTriple, ...],
) -> tuple[dict[str, object] | None, float]:
    quotes = {item.canonical_event_id: item for item in closing_quotes}
    labels: list[str] = []
    rows: list[list[float]] = []
    for item in test:
        quote = quotes.get(item.metadata.canonical_event_id)
        if quote is None:
            continue
        home, draw, away = closing_odds_to_normalized_probabilities(
            quote.home_odds,
            quote.draw_odds,
            quote.away_odds,
        )
        labels.append(item.result_code)
        rows.append([home, draw, away])
    coverage = len(labels) / float(len(test)) if test else 0.0
    if not labels:
        return None, coverage
    metrics = evaluate_probabilities(
        labels=tuple(labels),
        probabilities=np.asarray(rows, dtype=np.float64),
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    payload = metrics_to_json(metrics)
    payload["coverage"] = coverage
    payload["matched_events"] = len(labels)
    payload["test_events"] = len(test)
    payload["provider"] = "market-average"
    payload["quote_phase"] = "closing"
    payload["note"] = "External benchmark only. Football-Data ingestion time is not quote time."
    return payload, coverage


def _fold_evaluation_json(item: FoldEvaluation) -> dict[str, JsonValue]:
    payload = {
        "fold_id": item.fold_id,
        "train_date_range": list(item.train_date_range),
        "calibration_date_range": list(item.calibration_date_range),
        "test_date_range": list(item.test_date_range),
        "event_counts": item.event_counts,
        "class_counts": item.class_counts,
        "temperature": item.temperature,
        "uncalibrated_metrics": item.uncalibrated_metrics,
        "calibrated_metrics": item.calibrated_metrics,
        "market_benchmark_metrics": item.market_benchmark_metrics,
        "market_benchmark_coverage": item.market_benchmark_coverage,
        "calibration_improved_log_loss": item.calibration_improved_log_loss,
        "random_seed": item.random_seed,
        "feature_specification_version": item.feature_specification_version,
        "training_configuration": item.training_configuration,
    }
    value = ensure_json_value(payload)
    if not isinstance(value, dict):
        msg = "fold evaluation JSON must be an object"
        raise TrainingError(msg)
    return value

"""Unit tests for temporal validation, metrics, calibration, and artifacts."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from tests.helpers_training import synthetic_finished_events

from sports_analytics.core.exceptions import EvaluationError, ModelError
from sports_analytics.data.codec import utc_now
from sports_analytics.evaluation.metrics import (
    closing_odds_to_normalized_probabilities,
    evaluate_probabilities,
    multiclass_brier_score,
    multiclass_log_loss,
)
from sports_analytics.evaluation.temporal import TemporalSplitConfig, build_rolling_origin_folds
from sports_analytics.features.contracts import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.features.football.prematch import generate_prematch_features
from sports_analytics.models.artifacts import (
    build_model_document,
    infer_calibrated_probabilities,
    load_model_artifact,
    write_model_artifact,
)
from sports_analytics.models.calibration import fit_temperature, softmax
from sports_analytics.models.contracts import FOOTBALL_1X2_LOGISTIC_MODEL_V1, OUTCOME_LABELS_1X2
from sports_analytics.models.football_1x2 import evaluate_fold, prepare_folds, train_final_artifact
from sports_analytics.models.logistic import fit_multinomial_logistic


def test_chronological_folds_share_no_dates() -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=40, season_ids=("eng-premier-league:2023-2024",)
        )
    )
    folds = build_rolling_origin_folds(
        vectors,
        config=TemporalSplitConfig(
            min_train_rows=20,
            min_calibration_rows=8,
            min_test_rows=8,
            step_rows=10,
            maximum_folds=3,
        ),
    )
    assert folds
    for fold in folds:
        train_dates = {
            item.metadata.event_date
            for item in vectors
            if item.metadata.canonical_event_id in fold.train.event_ids
        }
        cal_dates = {
            item.metadata.event_date
            for item in vectors
            if item.metadata.canonical_event_id in fold.calibration.event_ids
        }
        test_dates = {
            item.metadata.event_date
            for item in vectors
            if item.metadata.canonical_event_id in fold.test.event_ids
        }
        assert not (train_dates & cal_dates)
        assert not (train_dates & test_dates)
        assert not (cal_dates & test_dates)
        assert fold.train.end_date < fold.calibration.start_date
        assert fold.calibration.end_date < fold.test.start_date


def test_training_requires_all_classes() -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=36, season_ids=("eng-premier-league:2023-2024",)
        )
    )
    folds = prepare_folds(
        vectors,
        config=TemporalSplitConfig(
            min_train_rows=18,
            min_calibration_rows=6,
            min_test_rows=6,
            step_rows=8,
            maximum_folds=2,
        ),
    )
    for fold in folds:
        assert all(fold.train.class_counts[label] >= 1 for label in OUTCOME_LABELS_1X2)


def test_no_random_split_api() -> None:
    source = Path("src/sports_analytics/evaluation/temporal.py").read_text(encoding="utf-8")
    assert "ShuffleSplit" not in source
    assert "train_test_split" not in source
    assert "KFold" not in source
    assert "rolling-origin" in source or "rolling_origin" in source


def test_metrics_and_probability_quality() -> None:
    labels = ("home", "draw", "away", "home")
    probs = np.asarray(
        [
            [0.7, 0.2, 0.1],
            [0.2, 0.6, 0.2],
            [0.1, 0.2, 0.7],
            [0.5, 0.25, 0.25],
        ],
        dtype=np.float64,
    )
    metrics = evaluate_probabilities(labels=labels, probabilities=probs)
    assert metrics.log_loss == multiclass_log_loss(labels=labels, probabilities=probs)
    assert metrics.brier_score == multiclass_brier_score(labels=labels, probabilities=probs)
    assert 0.0 <= metrics.accuracy <= 1.0
    assert metrics.expected_calibration_error >= 0.0
    assert len(metrics.calibration_bins) == 10


def test_temperature_selection_deterministic() -> None:
    logits = np.asarray(
        [
            [1.2, 0.1, -0.4],
            [0.2, 1.1, -0.3],
            [-0.2, 0.1, 1.3],
            [0.8, 0.5, 0.1],
            [0.3, 0.9, 0.2],
            [0.1, 0.2, 1.0],
        ],
        dtype=np.float64,
    )
    labels = ("home", "draw", "away", "home", "draw", "away")
    first = fit_temperature(logits=logits, labels=labels)
    second = fit_temperature(logits=logits, labels=labels)
    assert first.temperature == second.temperature
    probs = softmax(logits, temperature=first.temperature)
    assert probs.shape == (6, 3)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert np.isfinite(probs).all()


def test_closing_market_overround_removal_and_coverage() -> None:
    home, draw, away = closing_odds_to_normalized_probabilities(2.0, 3.5, 3.5)
    assert abs(home + draw + away - 1.0) < 1e-12
    assert home > draw
    with pytest.raises(EvaluationError):
        closing_odds_to_normalized_probabilities(1.0, 3.0, 3.0)


def test_inference_matches_training_and_rejects_corrupt_checksum(tmp_path: Path) -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=50,
            season_ids=("eng-premier-league:2022-2023", "eng-premier-league:2023-2024"),
        )
    )
    split = TemporalSplitConfig(
        min_train_rows=30,
        min_calibration_rows=10,
        min_test_rows=10,
        step_rows=15,
        maximum_folds=2,
    )
    models_root = tmp_path / "models"
    models_root.mkdir()
    result = train_final_artifact(
        vectors=vectors,
        folds=prepare_folds(vectors, config=split),
        closing_quotes=(),
        input_snapshots=[],
        competition_id="eng-premier-league",
        models_root=models_root,
        random_seed=42,
        split_config=split,
        artifact_id="00000000-0000-4000-8000-000000000007",
    )
    artifact = result.final_artifact
    sample = vectors[25]
    probs = infer_calibrated_probabilities(
        artifact=artifact,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        feature_values=sample.ordered_values(),
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
    )
    assert set(probs) == set(OUTCOME_LABELS_1X2)
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert not (models_root / result.final_artifact_relative_directory / "model.pkl").exists()
    assert not list(models_root.rglob("*.joblib"))
    assert not list(models_root.rglob("*.pkl"))

    with pytest.raises(ModelError, match="checksum"):
        load_model_artifact(
            models_root=models_root,
            relative_path=f"{result.final_artifact_relative_directory}/model.json",
            expected_checksum="0" * 64,
        )

    # Malformed artifact rejection.
    bad_dir = models_root / "bad"
    bad_dir.mkdir()
    (bad_dir / "model.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(ModelError, match="malformed"):
        load_model_artifact(models_root=models_root, relative_path="bad/model.json")


def test_fold_evaluation_records_uncalibrated_and_calibrated_metrics() -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=45, season_ids=("eng-premier-league:2023-2024",)
        )
    )
    folds = prepare_folds(
        vectors,
        config=TemporalSplitConfig(
            min_train_rows=20,
            min_calibration_rows=8,
            min_test_rows=8,
            step_rows=12,
            maximum_folds=1,
        ),
    )
    evaluation = evaluate_fold(
        fold=folds[0],
        vectors=vectors,
        closing_quotes=(),
        random_seed=7,
    )
    assert "log_loss" in evaluation.uncalibrated_metrics
    assert "log_loss" in evaluation.calibrated_metrics
    assert evaluation.feature_specification_version == FOOTBALL_1X2_PREMATCH_FEATURES_V1


def test_windows_safe_relative_model_paths(tmp_path: Path) -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=40, season_ids=("eng-premier-league:2023-2024",)
        )
    )
    train = vectors[:25]
    parameters = fit_multinomial_logistic(
        feature_matrix=np.asarray([item.ordered_values() for item in train], dtype=np.float64),
        labels=tuple(item.result_code for item in train),
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        random_seed=1,
    )
    document = build_model_document(
        artifact_id="00000000-0000-4000-8000-000000000008",
        parameters=parameters,
        temperature=1.0,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        model_specification_version=FOOTBALL_1X2_LOGISTIC_MODEL_V1,
        competition_id="eng-premier-league",
        trained_through_date=train[-1].metadata.event_date,
        calibrated_through_date=train[-1].metadata.event_date,
        input_snapshots=[],
        configuration={},
        validation_metrics={},
        evaluation_summary={},
        generated_at=utc_now(),
        random_seed=1,
    )
    models_root = tmp_path / "models"
    models_root.mkdir()
    relative = "football/football-1x2-logistic-v1/eng-premier-league/artifact-1"
    path, checksum = write_model_artifact(
        models_root=models_root,
        relative_directory=relative.replace("/", "\\") if False else relative,
        document=document,
    )
    assert path.as_posix().endswith("model.json")
    loaded = load_model_artifact(
        models_root=models_root,
        relative_path=relative.replace("/", "\\"),
        expected_checksum=checksum,
    )
    assert loaded.checksum_sha256 == checksum
    # Absolute paths rejected.
    with pytest.raises(ModelError, match="relative"):
        load_model_artifact(models_root=models_root, relative_path=str(path))

"""Unit tests for temporal validation, metrics, calibration, and artifacts."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from tests.helpers_training import synthetic_finished_events

from sports_analytics.core.exceptions import EvaluationError, ModelError
from sports_analytics.evaluation.metrics import (
    decimal_odds_to_normalized_probabilities,
    evaluate_probabilities,
    multiclass_brier_score,
    multiclass_log_loss,
    validate_probability_matrix,
)
from sports_analytics.evaluation.temporal import TemporalSplitConfig, build_rolling_origin_folds
from sports_analytics.features.contracts import OutcomeSpace
from sports_analytics.features.football.odds import closing_1x2_odds_to_normalized_probabilities
from sports_analytics.features.football.prematch import generate_prematch_features
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.models.artifacts import (
    FeatureArtifactLineage,
    build_model_document,
    derive_model_artifact_id,
    infer_calibrated_probabilities,
    load_model_artifact,
    write_model_artifact,
)
from sports_analytics.models.calibration import fit_temperature, softmax
from sports_analytics.models.football_1x2 import (
    FOOTBALL_1X2_MODEL_LIMITATIONS,
    OUTCOME_LABELS_1X2,
    evaluate_fold,
    football_1x2_logistic_specification,
    prepare_folds,
    train_final_artifact,
)
from sports_analytics.models.logistic import LogisticConfiguration, fit_multinomial_logistic


def _lineage() -> FeatureArtifactLineage:
    return FeatureArtifactLineage(
        feature_artifact_id="feature-1",
        feature_manifest_path="football/features/manifest.json",
        feature_manifest_checksum_sha256="a" * 64,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        fold_configuration=TemporalSplitConfig().to_json(),
        folds_file_checksum_sha256="b" * 64,
        input_snapshots=[],
    )


def test_chronological_folds_share_no_dates() -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=40, season_ids=("eng-premier-league:2023-2024",)
        )
    )
    folds = build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
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
    metrics = evaluate_probabilities(
        labels=labels,
        probabilities=probs,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    assert metrics.log_loss == multiclass_log_loss(
        labels=labels,
        probabilities=probs,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    assert metrics.brier_score == multiclass_brier_score(
        labels=labels,
        probabilities=probs,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
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
    first = fit_temperature(
        logits=logits,
        labels=labels,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    second = fit_temperature(
        logits=logits,
        labels=labels,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    assert first.temperature == second.temperature
    probs = softmax(
        logits,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        temperature=first.temperature,
    )
    assert probs.shape == (6, 3)
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert np.isfinite(probs).all()


def test_closing_market_overround_removal_and_coverage() -> None:
    home, draw, away = closing_1x2_odds_to_normalized_probabilities(2.0, 3.5, 3.5)
    assert abs(home + draw + away - 1.0) < 1e-12
    assert home > draw
    with pytest.raises(EvaluationError):
        closing_1x2_odds_to_normalized_probabilities(1.0, 3.0, 3.0)


def test_generic_two_outcome_odds_normalization() -> None:
    space = OutcomeSpace(ordered_labels=("no", "yes"))
    probs = decimal_odds_to_normalized_probabilities(
        outcome_space=space,
        decimal_odds=(2.0, 4.0),
    )
    assert len(probs) == 2
    assert abs(sum(probs) - 1.0) < 1e-12


def test_generic_four_outcome_odds_normalization() -> None:
    space = OutcomeSpace(ordered_labels=("a", "b", "c", "d"))
    probs = decimal_odds_to_normalized_probabilities(
        outcome_space=space,
        decimal_odds=(2.0, 3.0, 4.0, 5.0),
    )
    assert len(probs) == 4
    assert abs(sum(probs) - 1.0) < 1e-12


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
    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    result = train_final_artifact(
        vectors=vectors,
        folds=prepare_folds(vectors, config=split),
        closing_quotes=(),
        feature_lineage=_lineage(),
        competition_id="eng-premier-league",
        models_root=models_root,
        random_seed=42,
        split_config=split,
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
            specification=specification,
            expected_checksum="0" * 64,
        )

    bad_dir = models_root / "bad"
    bad_dir.mkdir()
    (bad_dir / "model.json").write_text("{not-json", encoding="utf-8")
    (bad_dir / "model_checksum.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(ModelError, match="checksum sidecar mismatch"):
        load_model_artifact(
            models_root=models_root,
            relative_path="bad/model.json",
            specification=specification,
        )


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
    config = LogisticConfiguration(random_seed=1)
    parameters = fit_multinomial_logistic(
        feature_matrix=np.asarray([item.ordered_values() for item in train], dtype=np.float64),
        labels=tuple(item.result_code for item in train),
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=config,
    )
    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    evaluation_summary: dict = {}
    scope_metadata = {"competition_id": "eng-premier-league", "model_scope": "competition"}
    artifact_id = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=scope_metadata,
        trained_through_date=train[-1].metadata.event_date,
        calibrated_through_date=train[-1].metadata.event_date,
        feature_lineage=_lineage(),
        evaluation_summary=evaluation_summary,
        random_seed=1,
    )
    document = build_model_document(
        artifact_id=artifact_id,
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=scope_metadata,
        trained_through_date=train[-1].metadata.event_date,
        calibrated_through_date=train[-1].metadata.event_date,
        feature_lineage=_lineage(),
        configuration={},
        validation_metrics={},
        evaluation_summary=evaluation_summary,
        random_seed=1,
        limitations=list(FOOTBALL_1X2_MODEL_LIMITATIONS),
    )
    models_root = tmp_path / "models"
    models_root.mkdir()
    relative = f"football/football-1x2-logistic-v1/eng-premier-league/{artifact_id}"
    path, checksum = write_model_artifact(
        models_root=models_root,
        relative_directory=relative,
        document=document,
        specification=specification,
    )
    assert path.as_posix().endswith("model.json")
    loaded = load_model_artifact(
        models_root=models_root,
        relative_path=relative.replace("/", "\\"),
        specification=specification,
        expected_checksum=checksum,
    )
    assert loaded.checksum_sha256 == checksum
    assert loaded.parameters.configuration.solver == "lbfgs"
    with pytest.raises(ModelError, match="relative"):
        load_model_artifact(
            models_root=models_root,
            relative_path=str(path),
            specification=specification,
        )


def test_generic_two_outcome_metrics_and_calibration() -> None:
    space = OutcomeSpace(ordered_labels=("yes", "no"))
    labels = ("yes", "no", "yes", "yes")
    probs = np.asarray([[0.8, 0.2], [0.4, 0.6], [0.55, 0.45], [0.7, 0.3]], dtype=np.float64)
    metrics = evaluate_probabilities(labels=labels, probabilities=probs, outcome_space=space)
    assert metrics.log_loss > 0.0
    logits = np.asarray([[0.5, -0.5], [-0.2, 0.2], [0.1, -0.1], [0.3, -0.3]], dtype=np.float64)
    result = fit_temperature(logits=logits, labels=labels, outcome_space=space)
    assert result.temperature > 0.0


def test_generic_four_outcome_probability_validation() -> None:
    space = OutcomeSpace(ordered_labels=("a", "b", "c", "d"))
    probs = np.asarray([[0.25, 0.25, 0.25, 0.25]], dtype=np.float64)
    validate_probability_matrix(probs, outcome_space=space)
    bad = np.asarray([[0.6, 0.6, 0.1, 0.1]], dtype=np.float64)
    with pytest.raises(EvaluationError, match="sum to one"):
        validate_probability_matrix(bad, outcome_space=space)


def test_most_recent_folds_retained_with_long_history() -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=120,
            season_ids=("eng-premier-league:2020-2021", "eng-premier-league:2021-2022"),
        )
    )
    config = TemporalSplitConfig(
        min_train_rows=30,
        min_calibration_rows=10,
        min_test_rows=10,
        step_rows=8,
        maximum_folds=3,
    )
    all_folds = build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        config=TemporalSplitConfig(
            min_train_rows=config.min_train_rows,
            min_calibration_rows=config.min_calibration_rows,
            min_test_rows=config.min_test_rows,
            step_rows=config.step_rows,
            maximum_folds=999,
        ),
    )
    retained = build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        config=config,
    )
    assert len(retained) == 3
    assert retained[-1].test.end_date == all_folds[-1].test.end_date
    assert retained[-1].test.end_date > all_folds[0].test.end_date
    latest_event_date = max(item.metadata.event_date for item in vectors)
    assert retained[-1].test.end_date == latest_event_date


def test_final_fold_reaches_latest_date_when_trailing_rows_below_step() -> None:
    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=80,
            season_ids=("eng-premier-league:2023-2024",),
        )
    )
    folds = build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        config=TemporalSplitConfig(
            min_train_rows=20,
            min_calibration_rows=8,
            min_test_rows=8,
            step_rows=25,
            maximum_folds=4,
        ),
    )
    assert folds[-1].test.end_date == max(item.event_date for item in vectors)

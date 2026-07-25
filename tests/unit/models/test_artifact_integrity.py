"""Regression tests for artifact immutability, verification, and determinism."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from tests.helpers_snapshots import database_path, prepare, publication_service
from tests.helpers_training import synthetic_finished_events, synthetic_season_csv

from sports_analytics.core.exceptions import FeatureError, ModelError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.football.datasets import (
    load_feature_artifact,
    snapshot_feature_artifact_bytes,
)
from sports_analytics.features.football.specification import FOOTBALL_1X2_FEATURE_NAMES_V1
from sports_analytics.models.artifacts import load_model_artifact
from sports_analytics.models.football_1x2 import football_1x2_logistic_specification
from sports_analytics.services.engine_cli import main as engine_main
from sports_analytics.services.training import (
    FeatureBuildRequest,
    TrainRequest,
    build_football_1x2_features,
    train_football_1x2_model,
)


def _publish_season(
    tmp_path: Path,
    *,
    snapshot_id: str,
    season_label: str,
    source_season_code: str,
    season_start_year: int,
) -> str:
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(
        tmp_path,
        snapshot_id=snapshot_id,
        snapshots_directory=snapshots_directory,
        season_label=season_label,
        source_season_code=source_season_code,
        content=synthetic_season_csv(
            season_start_year=season_start_year,
            match_count=30,
            include_closing_avg=True,
        ),
    )
    database = database_path(tmp_path / f"db-{snapshot_id}")
    service = publication_service(database, snapshots_directory)
    published = service.publish_or_reuse(
        prepared,
        actor="test",
        correlation_id=f"job-{snapshot_id}",
    )
    return published.snapshot_relative_path


def _paths(tmp_path: Path, *, features_dir: str = "features", models_dir: str = "models"):
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / features_dir),
            "SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY": str(tmp_path / models_dir),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(tmp_path / "operational.sqlite3"),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    paths.features_directory.mkdir(parents=True, exist_ok=True)
    paths.models_directory.mkdir(parents=True, exist_ok=True)
    return paths


def test_feature_artifact_immutable_through_training(tmp_path: Path) -> None:
    manifest_a = _publish_season(
        tmp_path,
        snapshot_id="11111111-1111-4111-8111-111111111111",
        season_label="2022-2023",
        source_season_code="2223",
        season_start_year=2022,
    )
    manifest_b = _publish_season(
        tmp_path,
        snapshot_id="22222222-2222-4222-8222-222222222222",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
    paths = _paths(tmp_path)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest_a, manifest_b),
            minimum_events=40,
            split_config=TemporalSplitConfig(
                min_train_rows=30,
                min_calibration_rows=10,
                min_test_rows=10,
                step_rows=12,
                maximum_folds=2,
            ),
        ),
    )
    before = snapshot_feature_artifact_bytes(artifact.directory)
    train_football_1x2_model(
        paths=paths,
        request=TrainRequest(
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum=artifact.manifest_checksum_sha256,
            random_seed=42,
        ),
    )
    after = snapshot_feature_artifact_bytes(artifact.directory)
    assert before == after


def test_feature_manifest_checksum_sidecar_required(tmp_path: Path) -> None:
    manifest = _publish_season(
        tmp_path,
        snapshot_id="33333333-3333-4333-8333-333333333333",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
    paths = _paths(tmp_path)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest,),
            minimum_events=30,
            split_config=TemporalSplitConfig(
                min_train_rows=15,
                min_calibration_rows=5,
                min_test_rows=5,
                maximum_folds=2,
            ),
        ),
    )
    sidecar = artifact.directory / "manifest_checksum.sha256"
    sidecar.unlink()
    with pytest.raises(FeatureError, match="checksum sidecar"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_feature_manifest_path_traversal_rejected(tmp_path: Path) -> None:
    manifest = _publish_season(
        tmp_path,
        snapshot_id="44444444-4444-4444-8444-444444444444",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
    paths = _paths(tmp_path)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest,),
            minimum_events=30,
            split_config=TemporalSplitConfig(
                min_train_rows=15,
                min_calibration_rows=5,
                min_test_rows=5,
                maximum_folds=2,
            ),
        ),
    )
    manifest_path = artifact.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = dict(payload["files"])
    files["../outside.parquet"] = files["features.parquet"]
    payload["files"] = files
    manifest_path.write_text(dumps_canonical_json(payload) + "\n", encoding="utf-8")
    checksum = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    (artifact.directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
    )
    with pytest.raises(FeatureError, match="exactly features"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_feature_target_id_mismatch_rejected(tmp_path: Path) -> None:
    manifest = _publish_season(
        tmp_path,
        snapshot_id="55555555-5555-4555-8555-555555555555",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
    paths = _paths(tmp_path)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest,),
            minimum_events=30,
            split_config=TemporalSplitConfig(
                min_train_rows=15,
                min_calibration_rows=5,
                min_test_rows=5,
                maximum_folds=2,
            ),
        ),
    )
    import pyarrow.parquet as pq

    targets_path = artifact.directory / "targets.parquet"
    table = pq.read_table(targets_path)
    rows = table.to_pylist()
    rows[0]["canonical_event_id"] = "missing-target-id"
    pq.write_table(__import__("pyarrow").Table.from_pylist(rows, schema=table.schema), targets_path)
    manifest_path = artifact.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest, size = __import__(
        "sports_analytics.snapshots.parquet", fromlist=["file_sha256_and_size"]
    ).file_sha256_and_size(targets_path)
    payload["files"]["targets.parquet"]["sha256"] = digest
    payload["files"]["targets.parquet"]["byte_count"] = size
    manifest_path.write_text(dumps_canonical_json(payload) + "\n", encoding="utf-8")
    checksum = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    (artifact.directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
    )
    with pytest.raises(FeatureError, match="match exactly"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_folds_unknown_event_rejected(tmp_path: Path) -> None:
    manifest = _publish_season(
        tmp_path,
        snapshot_id="66666666-6666-4666-8666-666666666666",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
    paths = _paths(tmp_path)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest,),
            minimum_events=30,
            split_config=TemporalSplitConfig(
                min_train_rows=15,
                min_calibration_rows=5,
                min_test_rows=5,
                maximum_folds=2,
            ),
        ),
    )
    import pyarrow as pa
    import pyarrow.parquet as pq

    folds_path = artifact.directory / "folds.parquet"
    table = pq.read_table(folds_path)
    rows = table.to_pylist()
    rows.append(
        {
            "fold_id": rows[0]["fold_id"],
            "region": "test",
            "canonical_event_id": "unknown-event-id",
            "event_date": rows[0]["event_date"],
        }
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), folds_path)
    manifest_path = artifact.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest, size = __import__(
        "sports_analytics.snapshots.parquet", fromlist=["file_sha256_and_size"]
    ).file_sha256_and_size(folds_path)
    payload["files"]["folds.parquet"]["sha256"] = digest
    payload["files"]["folds.parquet"]["byte_count"] = size
    payload["files"]["folds.parquet"]["row_count"] = len(rows)
    payload["row_counts"]["folds"] = len(rows)
    manifest_path.write_text(dumps_canonical_json(payload) + "\n", encoding="utf-8")
    checksum = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    (artifact.directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
    )
    with pytest.raises(FeatureError, match="unknown event"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_deterministic_feature_and_model_identity(tmp_path: Path) -> None:
    manifest_a = _publish_season(
        tmp_path,
        snapshot_id="77777777-7777-4777-8777-777777777777",
        season_label="2022-2023",
        source_season_code="2223",
        season_start_year=2022,
    )
    manifest_b = _publish_season(
        tmp_path,
        snapshot_id="88888888-8888-4888-8888-888888888888",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
    paths_one = _paths(tmp_path, features_dir="features-one", models_dir="models-one")
    paths_two = _paths(tmp_path, features_dir="features-two", models_dir="models-two")
    split = TemporalSplitConfig(
        min_train_rows=30,
        min_calibration_rows=10,
        min_test_rows=10,
        step_rows=12,
        maximum_folds=2,
    )
    first = build_football_1x2_features(
        paths=paths_one,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest_a, manifest_b),
            minimum_events=40,
            split_config=split,
        ),
    )
    second = build_football_1x2_features(
        paths=paths_two,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest_b, manifest_a),
            minimum_events=40,
            split_config=split,
        ),
    )
    assert first.artifact_id == second.artifact_id
    assert [item.ordered_values() for item in first.vectors] == [
        item.ordered_values() for item in second.vectors
    ]
    manifest_one, _, _, folds_one = load_feature_artifact(
        features_root=paths_one.features_directory,
        relative_directory=first.relative_directory,
        expected_manifest_checksum=first.manifest_checksum_sha256,
    )
    manifest_two, _, _, folds_two = load_feature_artifact(
        features_root=paths_two.features_directory,
        relative_directory=second.relative_directory,
        expected_manifest_checksum=second.manifest_checksum_sha256,
    )
    assert manifest_one == manifest_two
    assert [fold.fold_id for fold in folds_one] == [fold.fold_id for fold in folds_two]

    train_one = train_football_1x2_model(
        paths=paths_one,
        request=TrainRequest(
            feature_relative_directory=first.relative_directory,
            feature_manifest_checksum=first.manifest_checksum_sha256,
            random_seed=42,
        ),
    )
    train_two = train_football_1x2_model(
        paths=paths_two,
        request=TrainRequest(
            feature_relative_directory=second.relative_directory,
            feature_manifest_checksum=second.manifest_checksum_sha256,
            random_seed=42,
        ),
    )
    assert (
        train_one.final_artifact_relative_directory == train_two.final_artifact_relative_directory
    )
    assert train_one.final_artifact_checksum == train_two.final_artifact_checksum


def test_model_rejects_non_finite_coefficients(tmp_path: Path) -> None:
    vectors = synthetic_finished_events(
        matches_per_season=40, season_ids=("eng-premier-league:2023-2024",)
    )
    del vectors
    specification = football_1x2_logistic_specification("football-1x2-prematch-features-v1")
    models_root = tmp_path / "models"
    models_root.mkdir()
    model_dir = models_root / "bad-model"
    model_dir.mkdir()
    document = {
        "manifest_version": "model-manifest-v1",
        "identity_version": "model-identity-v1",
        "artifact_id": "bad",
        "artifact_type": "football-logistic-model",
        "model_specification_version": specification.model_specification_version,
        "feature_specification_version": specification.feature_specification_version,
        "sport_code": "football",
        "market_key": specification.market_key,
        "scope_metadata": {"competition_id": "eng-premier-league"},
        "limitations": [],
        "outcome_labels": ["home", "draw", "away"],
        "ordered_feature_names": list(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "scaler_mean": [0.0] * len(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "scaler_scale": [1.0] * len(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "coefficients": [[float("nan")] * len(FOOTBALL_1X2_FEATURE_NAMES_V1)] * 3,
        "intercepts": [0.0, 0.0, 0.0],
        "calibration_temperature": 1.0,
        "trained_through_date": "2023-08-01",
        "calibrated_through_date": "2023-08-31",
        "feature_artifact_id": "feature",
        "feature_manifest_path": "football/features/manifest.json",
        "feature_manifest_checksum_sha256": "a" * 64,
        "fold_configuration": TemporalSplitConfig().to_json(),
        "folds_file_checksum_sha256": "b" * 64,
        "input_snapshots": [],
        "configuration": {},
        "validation_metrics": {},
        "evaluation_summary": {},
        "random_seed": 1,
        "logistic_configuration": {
            "configuration_version": "logistic-configuration-v1",
            "solver": "lbfgs",
            "penalty": "l2",
            "regularization_strength": 1.0,
            "tolerance": 1e-4,
            "maximum_iterations": 2000,
            "fit_intercept": True,
            "random_seed": 1,
            "feature_scaler_policy": "standard-zero-scale-to-one",
            "sklearn_version": "1.0",
            "numpy_version": "1.0",
            "convergence_iterations": [10, 10, 10],
        },
        "serialization": {"pickle": False, "joblib": False},
    }
    raw = (json.dumps(document, allow_nan=True) + "\n").encode("utf-8")
    (model_dir / "model.json").write_bytes(raw)
    checksum = hashlib.sha256(raw).hexdigest()
    sidecar_path = model_dir / "model_checksum.sha256"
    sidecar_path.write_text(f"{checksum}\n", encoding="utf-8", newline="\n")
    with pytest.raises(ModelError, match="coefficients"):
        load_model_artifact(
            models_root=models_root,
            relative_path="bad-model/model.json",
            specification=specification,
        )


def test_model_rejects_malformed_dates(tmp_path: Path) -> None:
    specification = football_1x2_logistic_specification("football-1x2-prematch-features-v1")
    models_root = tmp_path / "models"
    model_dir = models_root / "bad-dates"
    model_dir.mkdir(parents=True)
    document = {
        "manifest_version": "model-manifest-v1",
        "identity_version": "model-identity-v1",
        "artifact_id": "bad",
        "artifact_type": "football-logistic-model",
        "model_specification_version": specification.model_specification_version,
        "feature_specification_version": specification.feature_specification_version,
        "sport_code": "football",
        "market_key": specification.market_key,
        "scope_metadata": {"competition_id": "eng-premier-league"},
        "limitations": [],
        "outcome_labels": ["home", "draw", "away"],
        "ordered_feature_names": list(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "scaler_mean": [0.0] * len(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "scaler_scale": [1.0] * len(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "coefficients": [[0.0] * len(FOOTBALL_1X2_FEATURE_NAMES_V1)] * 3,
        "intercepts": [0.0, 0.0, 0.0],
        "calibration_temperature": 1.0,
        "trained_through_date": "not-a-date",
        "calibrated_through_date": "2023-08-31",
        "feature_artifact_id": "feature",
        "feature_manifest_path": "football/features/manifest.json",
        "feature_manifest_checksum_sha256": "a" * 64,
        "fold_configuration": TemporalSplitConfig().to_json(),
        "folds_file_checksum_sha256": "b" * 64,
        "input_snapshots": [],
        "configuration": {},
        "validation_metrics": {},
        "evaluation_summary": {},
        "random_seed": 1,
        "logistic_configuration": {
            "configuration_version": "logistic-configuration-v1",
            "solver": "lbfgs",
            "penalty": "l2",
            "regularization_strength": 1.0,
            "tolerance": 1e-4,
            "maximum_iterations": 2000,
            "fit_intercept": True,
            "random_seed": 1,
            "feature_scaler_policy": "standard-zero-scale-to-one",
            "sklearn_version": "1.0",
            "numpy_version": "1.0",
            "convergence_iterations": [10, 10, 10],
        },
        "serialization": {"pickle": False, "joblib": False},
    }
    raw = (dumps_canonical_json(document) + "\n").encode("utf-8")
    (model_dir / "model.json").write_bytes(raw)
    checksum = hashlib.sha256(raw).hexdigest()
    sidecar_path = model_dir / "model_checksum.sha256"
    sidecar_path.write_text(f"{checksum}\n", encoding="utf-8", newline="\n")
    with pytest.raises(ModelError, match="trained_through_date"):
        load_model_artifact(
            models_root=models_root,
            relative_path="bad-dates/model.json",
            specification=specification,
        )


def test_engine_infer_malformed_json_returns_clean_error(
    tmp_path: Path,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    del isolated_cwd, clear_sports_analytics_env
    config = tmp_path / "settings.toml"
    config.write_text(
        "\n".join(
            [
                "[application]",
                'environment = "test"',
                "deterministic_seed = 42",
                "[storage]",
                f'root_directory = "{(tmp_path / "storage").as_posix()}"',
                f'snapshots_directory = "{(tmp_path / "snapshots").as_posix()}"',
                f'features_directory = "{(tmp_path / "features").as_posix()}"',
                f'models_directory = "{(tmp_path / "models").as_posix()}"',
                f'sqlite_path = "{(tmp_path / "operational.sqlite3").as_posix()}"',
                "[logging]",
                "file_enabled = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not-json", encoding="utf-8")
    code = engine_main(
        [
            "--config",
            str(config),
            "--infer-football-1x2",
            "--model",
            "football/model.json",
            "--feature-row-json",
            str(bad_json),
        ]
    )
    assert code != 0

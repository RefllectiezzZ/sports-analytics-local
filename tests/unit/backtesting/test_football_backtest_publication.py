"""End-to-end football closing backtest artifact publication tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers_snapshots import database_path, prepare, publication_service
from tests.helpers_training import synthetic_season_csv

from sports_analytics.artifact_schemas import validate_dataset_row_schema
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.football.datasets import load_feature_artifact
from sports_analytics.services.backtesting import (
    FOOTBALL_CLOSING_BACKTEST_SCHEMA,
    FootballBacktestRequest,
    run_and_publish_football_closing_backtest,
)
from sports_analytics.services.training import FeatureBuildRequest, build_football_1x2_features


def _runtime(tmp_path: Path):
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / "features"),
            "SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY": str(tmp_path / "exports"),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(tmp_path / "operational.sqlite3"),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    paths.features_directory.mkdir(parents=True, exist_ok=True)
    paths.exports_directory.mkdir(parents=True, exist_ok=True)
    return paths


def _feature_artifact(tmp_path: Path):
    manifest = prepare(
        tmp_path,
        snapshot_id="11111111-1111-4111-8111-111111111111",
        snapshots_directory=tmp_path / "snapshots",
        season_label="2023-2024",
        source_season_code="2324",
        content=synthetic_season_csv(
            season_start_year=2023,
            match_count=30,
            include_closing_avg=True,
        ),
    )
    database = database_path(tmp_path / "db")
    service = publication_service(database, tmp_path / "snapshots")
    published = service.publish_or_reuse(prepared=manifest, actor="test", correlation_id="job-1")
    paths = _runtime(tmp_path)
    artifact = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(published.snapshot_relative_path,),
            minimum_events=20,
            split_config=TemporalSplitConfig(
                min_train_rows=15,
                min_calibration_rows=5,
                min_test_rows=5,
                step_rows=4,
                maximum_folds=1,
            ),
        ),
    )
    _manifest, vectors, _quotes, _folds = load_feature_artifact(
        features_root=paths.features_directory,
        relative_directory=artifact.relative_directory,
        expected_manifest_checksum=artifact.manifest_checksum_sha256,
    )
    if not any(item.metadata.scheduled_start_utc is not None for item in vectors):
        pytest.skip("feature artifact lacks scheduled_start_utc required for closing benchmark")
    return paths, artifact


def test_football_backtest_publication_reload_and_tamper_rejection(tmp_path: Path) -> None:
    paths, artifact = _feature_artifact(tmp_path)
    published = run_and_publish_football_closing_backtest(
        paths=paths,
        request=FootballBacktestRequest(
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum=artifact.manifest_checksum_sha256,
            minimum_edge=-1.0,
            minimum_expected_value=-1.0,
            random_seed=42,
        ),
    )
    reloaded = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=published.artifact.relative_directory,
        expected_kind="backtest",
        expected_schema_version=FOOTBALL_CLOSING_BACKTEST_SCHEMA,
        expected_checksum=published.artifact.checksum_sha256,
        expected_artifact_id=published.artifact.artifact_id,
    )
    assert reloaded.artifact_id == published.artifact.artifact_id
    for name in (
        "predictions",
        "market_evaluations",
        "opportunities",
        "settlements",
        "aggregate_metrics",
    ):
        dataset = reloaded.dataset(name)
        assert dataset.row_count >= 1
        for row in dataset.rows:
            validate_dataset_row_schema(name, row, version=dataset.schema_version)
    prediction = reloaded.dataset("predictions").rows[0]
    tampered = dict(prediction)
    probabilities = list(tampered["probabilities"])
    first_probability = dict(probabilities[0])
    first_probability["probability"] = 0.99
    probabilities[0] = first_probability
    tampered["probabilities"] = probabilities
    with pytest.raises(ArtifactError):
        validate_dataset_row_schema(
            "predictions",
            tampered,
            version=reloaded.dataset("predictions").schema_version,
        )
    opportunity = reloaded.dataset("opportunities").rows[0]
    tampered_opportunity = dict(opportunity)
    tampered_opportunity["edge"] = float(opportunity["edge"]) + 0.5
    with pytest.raises(ArtifactError):
        validate_dataset_row_schema(
            "opportunities",
            tampered_opportunity,
            version=reloaded.dataset("opportunities").schema_version,
        )
    settlement = reloaded.dataset("settlements").rows[0]
    tampered_settlement = dict(settlement)
    tampered_settlement["profit_units"] = "9.9"
    with pytest.raises(ArtifactError):
        validate_dataset_row_schema(
            "settlements",
            tampered_settlement,
            version=reloaded.dataset("settlements").schema_version,
        )


def test_football_backtest_repeated_publication_is_deterministic(tmp_path: Path) -> None:
    paths, artifact = _feature_artifact(tmp_path)
    first = run_and_publish_football_closing_backtest(
        paths=paths,
        request=FootballBacktestRequest(
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum=artifact.manifest_checksum_sha256,
            minimum_edge=-1.0,
            minimum_expected_value=-1.0,
            random_seed=7,
        ),
    )
    # Keep the copied immutable artifact below the legacy Windows MAX_PATH limit.
    paths_two = _runtime(tmp_path / "r")
    paths_two.features_directory.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copytree(paths.features_directory, paths_two.features_directory, dirs_exist_ok=True)
    second = run_and_publish_football_closing_backtest(
        paths=paths_two,
        request=FootballBacktestRequest(
            feature_relative_directory=artifact.relative_directory,
            feature_manifest_checksum=artifact.manifest_checksum_sha256,
            minimum_edge=-1.0,
            minimum_expected_value=-1.0,
            random_seed=7,
        ),
    )
    assert first.artifact.artifact_id == second.artifact.artifact_id
    assert first.artifact.checksum_sha256 == second.artifact.checksum_sha256

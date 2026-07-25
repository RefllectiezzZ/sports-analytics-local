"""Integration tests for football 1X2 feature build and training workflow."""

from __future__ import annotations

import json
from pathlib import Path

from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.contracts import FOOTBALL_1X2_FEATURE_NAMES_V1
from sports_analytics.features.football.datasets import load_feature_artifact
from sports_analytics.services.engine_cli import main as engine_main
from sports_analytics.services.training import (
    FeatureBuildRequest,
    TrainRequest,
    build_football_1x2_features,
    train_football_1x2_model,
)
from tests.helpers_snapshots import database_path, prepare, publication_service
from tests.helpers_training import synthetic_season_csv


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


def test_multi_season_end_to_end_training(tmp_path: Path) -> None:
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
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / "features"),
            "SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY": str(tmp_path / "models"),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(tmp_path / "operational.sqlite3"),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    paths.features_directory.mkdir(parents=True, exist_ok=True)
    paths.models_directory.mkdir(parents=True, exist_ok=True)

    # Snapshot ordering must not change results.
    artifact_forward = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest_a, manifest_b),
            minimum_events=40,
            artifact_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ),
    )
    # Build a second artifact from reversed snapshot order into a different id.
    artifact_reverse = build_football_1x2_features(
        paths=paths,
        request=FeatureBuildRequest(
            relative_manifest_paths=(manifest_b, manifest_a),
            minimum_events=40,
            artifact_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        ),
    )
    assert [item.ordered_values() for item in artifact_forward.vectors] == [
        item.ordered_values() for item in artifact_reverse.vectors
    ]
    assert "result_code" not in (artifact_forward.directory / "features.parquet").name
    manifest, vectors, quotes = load_feature_artifact(
        features_root=paths.features_directory,
        relative_directory=artifact_forward.relative_directory,
        expected_manifest_checksum=artifact_forward.manifest_checksum_sha256,
    )
    assert manifest["feature_specification_version"]
    assert len(vectors) == artifact_forward.feature_row_count
    assert quotes  # closing averages present for coverage reporting

    result = train_football_1x2_model(
        paths=paths,
        request=TrainRequest(
            feature_relative_directory=artifact_forward.relative_directory,
            feature_manifest_checksum=artifact_forward.manifest_checksum_sha256,
            random_seed=42,
            split_config=TemporalSplitConfig(
                min_train_rows=30,
                min_calibration_rows=10,
                min_test_rows=10,
                step_rows=12,
                maximum_folds=2,
            ),
            artifact_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ),
    )
    assert result.folds
    assert result.final_artifact_checksum
    model_path = paths.models_directory / result.final_artifact_relative_directory / "model.json"
    assert model_path.is_file()
    document = json.loads(model_path.read_text(encoding="utf-8"))
    assert document["serialization"]["pickle"] is False
    assert document["serialization"]["joblib"] is False
    assert document["ordered_feature_names"] == list(FOOTBALL_1X2_FEATURE_NAMES_V1)
    for fold in result.folds:
        assert fold.market_benchmark_coverage >= 0.0
        if fold.market_benchmark_metrics is not None:
            assert "coverage" in fold.market_benchmark_metrics


def test_engine_cli_build_and_verify(
    tmp_path: Path,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    del isolated_cwd
    manifest = _publish_season(
        tmp_path,
        snapshot_id="33333333-3333-4333-8333-333333333333",
        season_label="2023-2024",
        source_season_code="2324",
        season_start_year=2023,
    )
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
    code = engine_main(
        [
            "--config",
            str(config),
            "--build-football-1x2-features",
            "--snapshot",
            manifest,
            "--minimum-events",
            "30",
        ]
    )
    assert code == 0

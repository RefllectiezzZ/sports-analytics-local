"""Focused tests for binary logistic, fold reconstruction, and model identity."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from tests.helpers_snapshots import database_path, prepare, publication_service
from tests.helpers_training import synthetic_season_csv

from sports_analytics.core.exceptions import FeatureError, ModelError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.contracts import OutcomeSpace
from sports_analytics.features.football.datasets import (
    load_feature_artifact,
)
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.models.artifacts import (
    FeatureArtifactLineage,
    build_model_document,
    derive_model_artifact_id,
    load_model_artifact,
    write_model_artifact,
)
from sports_analytics.models.calibration import softmax
from sports_analytics.models.football_1x2 import (
    FOOTBALL_1X2_MODEL_LIMITATIONS,
    football_1x2_logistic_specification,
)
from sports_analytics.models.logistic import (
    LogisticConfiguration,
    fit_multinomial_logistic,
    logits_from_parameters,
)
from sports_analytics.services.training import (
    FeatureBuildRequest,
    build_football_1x2_features,
)


def _binary_dataset(
    *,
    labels: tuple[str, ...],
    seed: int = 0,
) -> tuple[np.ndarray, tuple[str, ...]]:
    rng = np.random.default_rng(seed)
    matrix = rng.normal(size=(len(labels), 4))
    return matrix, labels


def test_binary_logistic_fit_and_numpy_matches_sklearn() -> None:
    space = OutcomeSpace(ordered_labels=("no", "yes"))
    labels = ("yes", "no", "yes", "yes", "no", "no", "yes", "no")
    matrix, labels = _binary_dataset(labels=labels, seed=3)
    config = LogisticConfiguration(random_seed=11)
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=("f0", "f1", "f2", "f3"),
        outcome_space=space,
        configuration=config,
    )
    assert parameters.outcome_labels == ("no", "yes")
    assert len(parameters.coefficients) == 2
    assert all(len(row) == 4 for row in parameters.coefficients)
    assert len(parameters.intercepts) == 2
    assert parameters.coefficients[0] == (0.0, 0.0, 0.0, 0.0)
    assert parameters.intercepts[0] == 0.0

    scaler = StandardScaler()
    scaled = scaler.fit_transform(matrix)
    scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    model = LogisticRegression(
        solver=config.solver,
        penalty=config.penalty,
        C=config.regularization_strength,
        tol=config.tolerance,
        max_iter=config.maximum_iterations,
        fit_intercept=config.fit_intercept,
        random_state=config.random_seed,
    )
    with pytest.warns(FutureWarning):
        model.fit(scaled, np.asarray(labels))
    sklearn_probs = model.predict_proba((matrix - scaler.mean_) / scale)
    class_index = {label: index for index, label in enumerate(model.classes_)}
    ordered_sklearn = sklearn_probs[:, [class_index["no"], class_index["yes"]]]
    logits = logits_from_parameters(feature_vector=matrix, parameters=parameters)
    numpy_probs = softmax(logits, outcome_space=space, temperature=1.0)
    assert np.allclose(numpy_probs, ordered_sklearn, atol=1e-12)


def test_binary_logistic_reversed_label_order() -> None:
    space = OutcomeSpace(ordered_labels=("yes", "no"))
    labels = ("yes", "no", "yes", "no", "yes", "no", "yes", "no")
    matrix, labels = _binary_dataset(labels=labels, seed=5)
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=("f0", "f1", "f2", "f3"),
        outcome_space=space,
        configuration=LogisticConfiguration(random_seed=4),
    )
    assert parameters.outcome_labels == ("yes", "no")
    assert len(parameters.coefficients) == 2
    zero_rows = [
        index
        for index, row in enumerate(parameters.coefficients)
        if row == (0.0, 0.0, 0.0, 0.0) and parameters.intercepts[index] == 0.0
    ]
    assert zero_rows == [parameters.outcome_labels.index("no")]
    scaler = StandardScaler()
    scaled = scaler.fit_transform(matrix)
    scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    config = LogisticConfiguration(random_seed=4)
    model = LogisticRegression(
        solver=config.solver,
        penalty=config.penalty,
        C=config.regularization_strength,
        tol=config.tolerance,
        max_iter=config.maximum_iterations,
        fit_intercept=config.fit_intercept,
        random_state=config.random_seed,
    )
    with pytest.warns(FutureWarning):
        model.fit(scaled, np.asarray(labels))
    sklearn_probs = model.predict_proba((matrix - scaler.mean_) / scale)
    class_index = {label: index for index, label in enumerate(model.classes_)}
    ordered_sklearn = sklearn_probs[:, [class_index["yes"], class_index["no"]]]
    logits = logits_from_parameters(feature_vector=matrix, parameters=parameters)
    probs = softmax(logits, outcome_space=space, temperature=1.0)
    assert np.allclose(probs, ordered_sklearn, atol=1e-12)


def test_four_outcome_logistic_fit_and_inference() -> None:
    space = OutcomeSpace(ordered_labels=("a", "b", "c", "d"))
    labels = ("a", "b", "c", "d", "a", "b", "c", "d", "a", "b", "c", "d")
    matrix, labels = _binary_dataset(labels=labels, seed=9)
    config = LogisticConfiguration(random_seed=8)
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=("f0", "f1", "f2", "f3"),
        outcome_space=space,
        configuration=config,
    )
    assert parameters.outcome_labels == ("a", "b", "c", "d")
    assert len(parameters.coefficients) == 4
    assert len(parameters.intercepts) == 4
    assert all(len(row) == 4 for row in parameters.coefficients)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(matrix)
    scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    model = LogisticRegression(
        solver=config.solver,
        penalty=config.penalty,
        C=config.regularization_strength,
        tol=config.tolerance,
        max_iter=config.maximum_iterations,
        fit_intercept=config.fit_intercept,
        random_state=config.random_seed,
    )
    with pytest.warns(FutureWarning):
        model.fit(scaled, np.asarray(labels))
    sklearn_probs = model.predict_proba((matrix - scaler.mean_) / scale)
    class_index = {label: index for index, label in enumerate(model.classes_)}
    ordered_sklearn = sklearn_probs[
        :,
        [class_index["a"], class_index["b"], class_index["c"], class_index["d"]],
    ]
    logits = logits_from_parameters(feature_vector=matrix, parameters=parameters)
    probs = softmax(logits, outcome_space=space, temperature=1.0)
    assert probs.shape == (12, 4)
    assert np.allclose(probs, ordered_sklearn, atol=1e-12)


def test_football_three_outcome_logistic_shape() -> None:
    from tests.helpers_training import synthetic_finished_events

    from sports_analytics.features.football.prematch import generate_prematch_features

    vectors = generate_prematch_features(
        synthetic_finished_events(
            matches_per_season=36, season_ids=("eng-premier-league:2023-2024",)
        )
    )[:30]
    parameters = fit_multinomial_logistic(
        feature_matrix=np.asarray([item.ordered_values() for item in vectors], dtype=np.float64),
        labels=tuple(item.result_code for item in vectors),
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=LogisticConfiguration(random_seed=2),
    )
    assert parameters.outcome_labels == ("home", "draw", "away")
    assert len(parameters.coefficients) == 3
    assert all(len(row) == len(FOOTBALL_1X2_FEATURE_NAMES_V1) for row in parameters.coefficients)


def _publish_season(
    tmp_path: Path,
    *,
    snapshot_id: str,
    season_label: str,
    code: str,
    year: int,
) -> str:
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(
        tmp_path,
        snapshot_id=snapshot_id,
        snapshots_directory=snapshots_directory,
        season_label=season_label,
        source_season_code=code,
        content=synthetic_season_csv(
            season_start_year=year,
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


def _paths(tmp_path: Path):
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
    return paths


def _built_artifact(tmp_path: Path):
    manifest = _publish_season(
        tmp_path,
        snapshot_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        season_label="2023-2024",
        code="2324",
        year=2023,
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
                step_rows=8,
                maximum_folds=2,
            ),
        ),
    )
    return paths, artifact


def test_reconstructed_class_counts_match_targets(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    _manifest, vectors, _quotes, folds = load_feature_artifact(
        features_root=paths.features_directory,
        relative_directory=artifact.relative_directory,
        expected_manifest_checksum=artifact.manifest_checksum_sha256,
    )
    by_id = {item.metadata.canonical_event_id: item.result_code for item in vectors}
    for fold in folds:
        for region in (fold.train, fold.calibration, fold.test):
            expected = {label: 0 for label in FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels}
            for event_id in region.event_ids:
                expected[by_id[event_id]] += 1
            assert region.class_counts == expected
            assert sum(region.class_counts.values()) == len(region.event_ids)


def test_fold_date_mismatch_rejected(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    folds_path = artifact.directory / "folds.parquet"
    table = pq.read_table(folds_path)
    rows = table.to_pylist()
    rows[0]["event_date"] = date(1999, 1, 1)
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), folds_path)
    _rewrite_file_checksum(artifact.directory, "folds.parquet", row_count=len(rows))
    with pytest.raises(FeatureError, match="does not match feature event date"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_duplicate_fold_assignment_rejected(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    folds_path = artifact.directory / "folds.parquet"
    table = pq.read_table(folds_path)
    rows = table.to_pylist()
    rows.append(dict(rows[0]))
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), folds_path)
    _rewrite_file_checksum(artifact.directory, "folds.parquet", row_count=len(rows))
    with pytest.raises(FeatureError, match="duplicate events"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_event_in_multiple_regions_rejected(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    folds_path = artifact.directory / "folds.parquet"
    table = pq.read_table(folds_path)
    rows = table.to_pylist()
    train_row = next(row for row in rows if row["region"] == "train")
    test_row = next(row for row in rows if row["region"] == "test")
    rows.append(
        {
            "fold_id": train_row["fold_id"],
            "region": "test",
            "canonical_event_id": train_row["canonical_event_id"],
            "event_date": train_row["event_date"],
        }
    )
    del test_row
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), folds_path)
    _rewrite_file_checksum(artifact.directory, "folds.parquet", row_count=len(rows))
    with pytest.raises(FeatureError, match="multiple regions"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_invalid_target_label_rejected(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    targets_path = artifact.directory / "targets.parquet"
    table = pq.read_table(targets_path)
    rows = table.to_pylist()
    rows[0]["result_code"] = "bogus"
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), targets_path)
    _rewrite_file_checksum(artifact.directory, "targets.parquet", row_count=len(rows))
    with pytest.raises(FeatureError, match="invalid target label"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_fold_summary_mismatch_rejected(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    manifest_path = artifact.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["fold_summaries"][0]["train"]["event_count"] = 1
    manifest_path.write_text(dumps_canonical_json(payload) + "\n", encoding="utf-8")
    checksum = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (artifact.directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(FeatureError, match="fold summaries"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_final_fold_before_latest_date_rejected(tmp_path: Path) -> None:
    paths, artifact = _built_artifact(tmp_path)
    folds_path = artifact.directory / "folds.parquet"
    table = pq.read_table(folds_path)
    rows = table.to_pylist()
    final_fold_id = sorted({str(row["fold_id"]) for row in rows})[-1]
    latest_date = max(
        row["event_date"]
        for row in rows
        if row["fold_id"] == final_fold_id and row["region"] == "test"
    )
    filtered = [
        row
        for row in rows
        if not (
            row["fold_id"] == final_fold_id
            and row["region"] == "test"
            and row["event_date"] == latest_date
        )
    ]
    pq.write_table(pa.Table.from_pylist(filtered, schema=table.schema), folds_path)
    _rewrite_file_checksum(artifact.directory, "folds.parquet", row_count=len(filtered))
    with pytest.raises(FeatureError, match="latest feature date"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_model_identity_changes_with_parameters_and_metadata(tmp_path: Path) -> None:
    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    config = LogisticConfiguration(random_seed=3)
    labels = ("home", "draw", "away", "home", "draw", "away", "home", "draw", "away")
    matrix = np.random.default_rng(1).normal(size=(len(labels), len(FOOTBALL_1X2_FEATURE_NAMES_V1)))
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=config,
    )
    lineage = FeatureArtifactLineage(
        feature_artifact_id="feature-1",
        feature_manifest_path="football/features/manifest.json",
        feature_manifest_checksum_sha256="a" * 64,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        fold_configuration=TemporalSplitConfig().to_json(),
        folds_file_checksum_sha256="b" * 64,
        input_snapshots=[],
    )
    evaluation_summary = {"fold_count": 1}
    base_id = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    mutated_coef = parameters.coefficients[0]
    changed_coefficients = (
        (mutated_coef[0] + 0.1, *mutated_coef[1:]),
        *parameters.coefficients[1:],
    )
    from dataclasses import replace

    changed_params = replace(parameters, coefficients=changed_coefficients)
    assert (
        derive_model_artifact_id(
            specification=specification,
            parameters=changed_params,
            temperature=1.0,
            trained_through_date=date(2023, 8, 1),
            calibrated_through_date=date(2023, 8, 31),
            feature_lineage=lineage,
            evaluation_summary=evaluation_summary,
            random_seed=3,
        )
        != base_id
    )
    assert (
        derive_model_artifact_id(
            specification=specification,
            parameters=parameters,
            temperature=1.25,
            trained_through_date=date(2023, 8, 1),
            calibrated_through_date=date(2023, 8, 31),
            feature_lineage=lineage,
            evaluation_summary=evaluation_summary,
            random_seed=3,
        )
        != base_id
    )
    assert (
        derive_model_artifact_id(
            specification=specification,
            parameters=parameters,
            temperature=1.0,
            trained_through_date=date(2023, 8, 2),
            calibrated_through_date=date(2023, 8, 31),
            feature_lineage=lineage,
            evaluation_summary=evaluation_summary,
            random_seed=3,
        )
        != base_id
    )
    version_changed = replace(parameters, sklearn_version="9.9.9")
    assert (
        derive_model_artifact_id(
            specification=specification,
            parameters=version_changed,
            temperature=1.0,
            trained_through_date=date(2023, 8, 1),
            calibrated_through_date=date(2023, 8, 31),
            feature_lineage=lineage,
            evaluation_summary=evaluation_summary,
            random_seed=3,
        )
        != base_id
    )

    document = build_model_document(
        artifact_id=base_id,
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata={"competition_id": "eng-premier-league", "model_scope": "competition"},
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        configuration={},
        validation_metrics={},
        evaluation_summary=evaluation_summary,
        random_seed=3,
        limitations=list(FOOTBALL_1X2_MODEL_LIMITATIONS),
    )
    models_root = tmp_path / "models"
    models_root.mkdir()

    def _write_and_assert_identity_rejected(payload: dict) -> None:
        text = dumps_canonical_json(payload) + "\n"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        relative = f"football/model/{digest[:12]}"
        model_dir = models_root / relative
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "model.json").write_text(text, encoding="utf-8", newline="\n")
        (model_dir / "model_checksum.sha256").write_text(
            f"{digest}\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ModelError, match="artifact_id"):
            load_model_artifact(
                models_root=models_root,
                relative_path=f"{relative}/model.json",
                specification=specification,
            )

    relative = f"football/model/{base_id}"
    write_model_artifact(
        models_root=models_root,
        relative_directory=relative,
        document=document,
        specification=specification,
    )
    loaded = load_model_artifact(
        models_root=models_root,
        relative_path=f"{relative}/model.json",
        specification=specification,
    )
    assert loaded.document["artifact_id"] == base_id

    coefficient_payload = json.loads(dumps_canonical_json(document))
    coefficient_payload["coefficients"][0][0] = (
        float(coefficient_payload["coefficients"][0][0]) + 0.5
    )
    _write_and_assert_identity_rejected(coefficient_payload)

    temperature_payload = json.loads(dumps_canonical_json(document))
    temperature_payload["calibration_temperature"] = 1.25
    _write_and_assert_identity_rejected(temperature_payload)

    cutoff_payload = json.loads(dumps_canonical_json(document))
    cutoff_payload["trained_through_date"] = "2023-08-02"
    _write_and_assert_identity_rejected(cutoff_payload)

    version_payload = json.loads(dumps_canonical_json(document))
    version_payload["logistic_configuration"]["sklearn_version"] = "9.9.9"
    _write_and_assert_identity_rejected(version_payload)


def _rewrite_file_checksum(directory: Path, filename: str, *, row_count: int) -> None:
    from sports_analytics.snapshots.parquet import file_sha256_and_size

    path = directory / filename
    digest, size = file_sha256_and_size(path)
    manifest_path = directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"][filename]["sha256"] = digest
    payload["files"][filename]["byte_count"] = size
    payload["files"][filename]["row_count"] = row_count
    key = filename.replace(".parquet", "")
    if key in payload.get("row_counts", {}):
        payload["row_counts"][key] = row_count
    manifest_path.write_text(dumps_canonical_json(payload) + "\n", encoding="utf-8")
    checksum = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
        newline="\n",
    )

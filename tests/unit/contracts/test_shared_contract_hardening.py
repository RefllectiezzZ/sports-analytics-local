"""Regression tests for shared-contract hardening and strict artifact validation."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.helpers_snapshots import database_path, prepare, publication_service
from tests.helpers_training import synthetic_season_csv
from tests.unit.models.test_import_boundary import _module_paths

from sports_analytics.core.exceptions import EvaluationError, FeatureError, ModelError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.evaluation.metrics import decimal_odds_to_normalized_probabilities
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.contracts import (
    FEATURE_SCOPE_VERSION_PREFIX,
    FeatureRowMetadata,
    FeatureSpecification,
    OutcomeSpace,
    validate_feature_scope_key,
)
from sports_analytics.features.football.metadata import FootballFeatureRowMetadata
from sports_analytics.features.football.odds import closing_1x2_odds_to_normalized_probabilities
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.models.artifacts import (
    MODEL_CHECKSUM_SIDECAR,
    MODEL_IDENTITY_VERSION,
    FeatureArtifactLineage,
    build_model_document,
    derive_model_artifact_id,
    load_model_artifact,
    validate_feature_artifact_lineage,
    write_model_artifact,
)
from sports_analytics.models.football_1x2 import (
    FOOTBALL_1X2_MODEL_LIMITATIONS,
    football_1x2_logistic_specification,
)
from sports_analytics.models.logistic import LogisticConfiguration, fit_multinomial_logistic
from sports_analytics.services.training import FeatureBuildRequest, build_football_1x2_features

_DEFAULT_SCOPE_METADATA = {"competition_id": "eng-premier-league", "model_scope": "competition"}


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


def _valid_model_document(tmp_path: Path | None = None) -> tuple[dict, object]:
    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    labels = ("home", "draw", "away", "home", "draw", "away", "home", "draw", "away")
    matrix = np.random.default_rng(1).normal(size=(len(labels), len(FOOTBALL_1X2_FEATURE_NAMES_V1)))
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=LogisticConfiguration(random_seed=3),
    )
    evaluation_summary = {"fold_count": 1}
    artifact_id = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=_DEFAULT_SCOPE_METADATA,
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=_lineage(),
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    document = build_model_document(
        artifact_id=artifact_id,
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=_DEFAULT_SCOPE_METADATA,
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=_lineage(),
        configuration={},
        validation_metrics={},
        evaluation_summary=evaluation_summary,
        random_seed=3,
        limitations=list(FOOTBALL_1X2_MODEL_LIMITATIONS),
    )
    return document, specification


def test_generic_feature_metadata_has_no_football_roles() -> None:
    metadata = FeatureRowMetadata(
        canonical_event_id="event-1",
        event_date=date(2023, 8, 1),
        scheduled_start_utc=None,
        feature_cutoff_date=date(2023, 8, 1),
        feature_specification_version="spec-v1",
        scope_metadata={"domain": "example"},
    )
    assert metadata.scope_metadata == {"domain": "example"}
    assert not hasattr(metadata, "competition_id")
    assert not hasattr(metadata, "home_canonical_participant_id")


def test_football_metadata_extension_available() -> None:
    football = FootballFeatureRowMetadata.create(
        canonical_event_id="event-1",
        competition_id="eng-premier-league",
        season_id="eng-premier-league:2023-2024",
        event_date=date(2023, 8, 1),
        scheduled_start_utc=None,
        feature_cutoff_date=date(2023, 8, 1),
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        home_canonical_participant_id="home-1",
        away_canonical_participant_id="away-1",
    )
    assert football.competition_id == "eng-premier-league"
    assert football.base.scope_metadata["competition_id"] == "eng-premier-league"


def test_extensible_feature_scope_keys() -> None:
    for scope in (
        f"{FEATURE_SCOPE_VERSION_PREFIX}team",
        f"{FEATURE_SCOPE_VERSION_PREFIX}participant",
        f"{FEATURE_SCOPE_VERSION_PREFIX}event",
        f"{FEATURE_SCOPE_VERSION_PREFIX}market",
        f"{FEATURE_SCOPE_VERSION_PREFIX}selection",
    ):
        validate_feature_scope_key(scope)
    with pytest.raises(FeatureError):
        validate_feature_scope_key("team")
    specification = FeatureSpecification(
        specification_version="spec-v1",
        sport_code="example",
        market_key="example.market",
        feature_scope=f"{FEATURE_SCOPE_VERSION_PREFIX}market",
        ordered_feature_names=("feature_a",),
        metadata_columns=("canonical_event_id",),
        description="example",
    )
    assert specification.feature_scope.endswith("market")


def test_binary_liblinear_accepted() -> None:
    space = OutcomeSpace(ordered_labels=("no", "yes"))
    labels = ("yes", "no", "yes", "no", "yes", "no", "yes", "no")
    matrix = np.random.default_rng(2).normal(size=(len(labels), 3))
    config = LogisticConfiguration(solver="liblinear", penalty="l2", random_seed=4)
    config.validate(outcome_count=2)
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=("f0", "f1", "f2"),
        outcome_space=space,
        configuration=config,
    )
    assert len(parameters.coefficients) == 2


def test_liblinear_rejected_for_three_outcomes() -> None:
    config = LogisticConfiguration(solver="liblinear", penalty="l2", random_seed=4)
    with pytest.raises(ModelError, match="liblinear"):
        config.validate(outcome_count=3)


def test_elasticnet_configuration_rejected() -> None:
    config = LogisticConfiguration(solver="saga", penalty="elasticnet", random_seed=4)
    with pytest.raises(ModelError, match="unsupported logistic solver/penalty"):
        config.validate(outcome_count=3)


def test_string_false_fit_intercept_rejected_on_load(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["logistic_configuration"]["fit_intercept"] = "false"
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="fit_intercept"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_boolean_seed_rejected_on_load(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["random_seed"] = True
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="random_seed"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_non_finite_scaler_mean_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["scaler_mean"][0] = "nan"
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="scaler_mean"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_zero_scaler_scale_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["scaler_scale"][0] = 0.0
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="scaler_scale"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_invalid_convergence_iterations_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["logistic_configuration"]["convergence_iterations"] = []
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="convergence_iterations"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_malformed_lineage_checksum_rejected() -> None:
    lineage = FeatureArtifactLineage(
        feature_artifact_id="feature-1",
        feature_manifest_path="football/features/manifest.json",
        feature_manifest_checksum_sha256="not-a-checksum",
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        fold_configuration=TemporalSplitConfig().to_json(),
        folds_file_checksum_sha256="b" * 64,
        input_snapshots=[],
    )
    with pytest.raises(ModelError, match="lineage checksum"):
        validate_feature_artifact_lineage(
            lineage,
            expected_feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        )


def test_lineage_path_traversal_rejected() -> None:
    lineage = FeatureArtifactLineage(
        feature_artifact_id="feature-1",
        feature_manifest_path="../escape/manifest.json",
        feature_manifest_checksum_sha256="a" * 64,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        fold_configuration=TemporalSplitConfig().to_json(),
        folds_file_checksum_sha256="b" * 64,
        input_snapshots=[],
    )
    with pytest.raises(ModelError, match="traverse"):
        validate_feature_artifact_lineage(
            lineage,
            expected_feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        )


def test_missing_checksum_sidecar_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    models_root = tmp_path / "models"
    model_dir = models_root / "bad"
    model_dir.mkdir(parents=True)
    text = dumps_canonical_json(document) + "\n"
    (model_dir / "model.json").write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(ModelError, match="checksum sidecar"):
        load_model_artifact(
            models_root=models_root,
            relative_path="bad/model.json",
            specification=specification,
        )


def test_symlink_checksum_sidecar_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    models_root = tmp_path / "models"
    model_dir = models_root / "bad"
    model_dir.mkdir(parents=True)
    text = dumps_canonical_json(document) + "\n"
    (model_dir / "model.json").write_text(text, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sidecar = model_dir / MODEL_CHECKSUM_SIDECAR
    sidecar.write_text(f"{digest}\n", encoding="utf-8", newline="\n")
    original_lstat = Path.lstat

    def fake_lstat(candidate: Path) -> object:
        if candidate == sidecar:
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        return original_lstat(candidate)

    with patch.object(Path, "lstat", fake_lstat):
        with pytest.raises(ModelError, match="symlink"):
            load_model_artifact(
                models_root=models_root,
                relative_path="bad/model.json",
                specification=specification,
            )


def test_altered_feature_parquet_schema_rejected(tmp_path: Path) -> None:
    paths, artifact = _loadable_artifact(tmp_path)
    features_path = artifact.directory / "features.parquet"
    table = pq.read_table(features_path)
    rows = table.to_pylist()
    altered_schema = table.schema.set(
        table.schema.get_field_index("home_elo"),
        pa.field("home_elo", pa.float32(), nullable=False),
    )
    pq.write_table(pa.Table.from_pylist(rows, schema=altered_schema), features_path)
    _rewrite_file_checksum(artifact.directory, "features.parquet", row_count=len(rows))
    from sports_analytics.features.football.datasets import load_feature_artifact

    with pytest.raises(FeatureError, match="schema mismatch"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_missing_fold_summaries_rejected(tmp_path: Path) -> None:
    paths, artifact = _loadable_artifact(tmp_path)
    manifest_path = artifact.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["fold_summaries"] = []
    manifest_path.write_text(dumps_canonical_json(payload) + "\n", encoding="utf-8")
    checksum = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (artifact.directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
        newline="\n",
    )
    from sports_analytics.features.football.datasets import load_feature_artifact

    with pytest.raises(FeatureError, match="fold_summaries"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


def test_atomic_model_publication(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    models_root = tmp_path / "models"
    models_root.mkdir()
    relative = "football/model/atomic-test"
    path, checksum = write_model_artifact(
        models_root=models_root,
        relative_directory=relative,
        document=document,
        specification=specification,
    )
    assert path.is_file()
    loaded = load_model_artifact(
        models_root=models_root,
        relative_path=f"{relative}/model.json",
        specification=specification,
        expected_checksum=checksum,
    )
    assert loaded.document["identity_version"] == MODEL_IDENTITY_VERSION


def test_no_partial_final_model_directory_after_failure(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    models_root = tmp_path / "models"
    models_root.mkdir()
    relative = "football/model/failed-publish"
    final_directory = models_root / "football" / "model" / "failed-publish"
    original_write_text = Path.write_text
    calls = {"count": 0}

    def flaky_write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if self.name == MODEL_CHECKSUM_SIDECAR:
            raise OSError("sidecar failed")
        return original_write_text(self, *args, **kwargs)

    with patch.object(Path, "write_text", flaky_write_text):
        with pytest.raises(OSError, match="sidecar failed"):
            write_model_artifact(
                models_root=models_root,
                relative_directory=relative,
                document=document,
                specification=specification,
            )
    assert not final_directory.exists()
    temp_dirs = [path for path in models_root.iterdir() if path.name.startswith(".model-")]
    assert temp_dirs == []


def _identity_parameters() -> tuple[object, object, FeatureArtifactLineage, dict]:
    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    labels = ("home", "draw", "away", "home", "draw", "away", "home", "draw", "away")
    matrix = np.random.default_rng(9).normal(size=(len(labels), len(FOOTBALL_1X2_FEATURE_NAMES_V1)))
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=LogisticConfiguration(random_seed=3),
    )
    return specification, parameters, _lineage(), {"fold_count": 1}


def test_scope_metadata_included_in_model_identity() -> None:
    specification, parameters, lineage, evaluation_summary = _identity_parameters()
    kwargs = {
        "specification": specification,
        "parameters": parameters,
        "temperature": 1.0,
        "trained_through_date": date(2023, 8, 1),
        "calibrated_through_date": date(2023, 8, 31),
        "feature_lineage": lineage,
        "evaluation_summary": evaluation_summary,
        "random_seed": 3,
    }
    first = derive_model_artifact_id(scope_metadata=_DEFAULT_SCOPE_METADATA, **kwargs)
    second = derive_model_artifact_id(scope_metadata=_DEFAULT_SCOPE_METADATA, **kwargs)
    changed_scope = derive_model_artifact_id(
        scope_metadata={"competition_id": "eng-premier-league", "model_scope": "global"},
        **kwargs,
    )
    assert first == second
    assert first != changed_scope


def test_changing_competition_scope_invalidates_model_id() -> None:
    specification, parameters, lineage, evaluation_summary = _identity_parameters()
    base = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=_DEFAULT_SCOPE_METADATA,
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    changed = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata={"competition_id": "esp-la-liga", "model_scope": "competition"},
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    assert base != changed


def test_scope_metadata_insertion_order_does_not_change_model_id() -> None:
    specification, parameters, lineage, evaluation_summary = _identity_parameters()
    first = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata={"competition_id": "eng-premier-league", "model_scope": "competition"},
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    second = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata={"model_scope": "competition", "competition_id": "eng-premier-league"},
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    assert first == second


@pytest.mark.parametrize(
    "scope_metadata",
    [
        [],
        "competition",
        {"": "eng-premier-league"},
        {"competition_id": {1, 2, 3}},
    ],
)
def test_malformed_scope_metadata_rejected(scope_metadata: object) -> None:
    specification, parameters, lineage, evaluation_summary = _identity_parameters()
    with pytest.raises(ModelError, match="scope_metadata"):
        derive_model_artifact_id(
            specification=specification,
            parameters=parameters,
            temperature=1.0,
            scope_metadata=scope_metadata,  # type: ignore[arg-type]
            trained_through_date=date(2023, 8, 1),
            calibrated_through_date=date(2023, 8, 31),
            feature_lineage=lineage,
            evaluation_summary=evaluation_summary,
            random_seed=3,
        )


def test_mapping_extra_outcome_rejected() -> None:
    space = OutcomeSpace(ordered_labels=("home", "draw", "away"))
    with pytest.raises(EvaluationError, match="unexpected decimal odds outcome"):
        decimal_odds_to_normalized_probabilities(
            outcome_space=space,
            decimal_odds={"home": 2.0, "draw": 3.0, "away": 4.0, "extra": 5.0},
        )


def test_mapping_missing_outcome_rejected() -> None:
    space = OutcomeSpace(ordered_labels=("home", "draw", "away"))
    with pytest.raises(EvaluationError, match="missing decimal odds for outcome"):
        decimal_odds_to_normalized_probabilities(
            outcome_space=space,
            decimal_odds={"home": 2.0, "draw": 3.0},
        )


def test_mapping_insertion_order_does_not_alter_canonical_output() -> None:
    space = OutcomeSpace(ordered_labels=("home", "draw", "away"))
    canonical = decimal_odds_to_normalized_probabilities(
        outcome_space=space,
        decimal_odds={"home": 2.0, "draw": 3.0, "away": 4.0},
    )
    reversed_insertion = decimal_odds_to_normalized_probabilities(
        outcome_space=space,
        decimal_odds={"away": 4.0, "draw": 3.0, "home": 2.0},
    )
    assert canonical == reversed_insertion


def test_boolean_odds_rejected() -> None:
    space = OutcomeSpace(ordered_labels=("home", "draw", "away"))
    with pytest.raises(EvaluationError, match="invalid decimal odds"):
        decimal_odds_to_normalized_probabilities(
            outcome_space=space,
            decimal_odds={"home": True, "draw": 3.0, "away": 4.0},  # type: ignore[dict-item]
        )
    with pytest.raises(EvaluationError, match="invalid decimal odds"):
        decimal_odds_to_normalized_probabilities(
            outcome_space=space,
            decimal_odds=(2.0, True, 4.0),  # type: ignore[list-item]
        )


def test_numeric_string_odds_rejected() -> None:
    space = OutcomeSpace(ordered_labels=("home", "draw", "away"))
    with pytest.raises(EvaluationError, match="invalid decimal odds"):
        decimal_odds_to_normalized_probabilities(
            outcome_space=space,
            decimal_odds={"home": "2.0", "draw": 3.0, "away": 4.0},  # type: ignore[dict-item]
        )
    with pytest.raises(EvaluationError, match="invalid decimal odds"):
        decimal_odds_to_normalized_probabilities(
            outcome_space=space,
            decimal_odds=("2.0", 3.0, 4.0),  # type: ignore[list-item]
        )


def test_football_1x2_benchmark_unchanged() -> None:
    home, draw, away = closing_1x2_odds_to_normalized_probabilities(2.0, 3.5, 3.5)
    assert abs(home + draw + away - 1.0) < 1e-12
    assert home > draw


def test_integer_trained_through_date_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["trained_through_date"] = 20230801
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="trained_through_date"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_compact_trained_through_date_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["trained_through_date"] = "20230801"
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="trained_through_date"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_non_string_feature_artifact_id_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["feature_artifact_id"] = 12345
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="feature_artifact_id"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_feature_manifest_path_must_end_with_manifest_json(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    document["feature_manifest_path"] = "football/features/manifest.txt"
    _write_raw_model(tmp_path, document=document)
    with pytest.raises(ModelError, match="manifest.json"):
        load_model_artifact(
            models_root=tmp_path / "models",
            relative_path="bad/model.json",
            specification=specification,
        )


def test_model_checksum_sidecar_malformed_digest_rejected(tmp_path: Path) -> None:
    document, specification = _valid_model_document()
    models_root = tmp_path / "models"
    model_dir = models_root / "bad"
    model_dir.mkdir(parents=True)
    text = dumps_canonical_json(document) + "\n"
    (model_dir / "model.json").write_text(text, encoding="utf-8", newline="\n")
    (model_dir / MODEL_CHECKSUM_SIDECAR).write_text("not-a-checksum\n", encoding="utf-8")
    with pytest.raises(ModelError, match="checksum sidecar digest"):
        load_model_artifact(
            models_root=models_root,
            relative_path="bad/model.json",
            specification=specification,
        )


def test_feature_checksum_sidecar_malformed_digest_rejected(tmp_path: Path) -> None:
    paths, artifact = _loadable_artifact(tmp_path)
    sidecar = artifact.directory / "manifest_checksum.sha256"
    sidecar.write_text("not-a-checksum\n", encoding="utf-8")
    from sports_analytics.features.football.datasets import load_feature_artifact

    with pytest.raises(FeatureError, match="checksum sidecar digest"):
        load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=artifact.relative_directory,
        )


@pytest.mark.parametrize("module_path", _module_paths(), ids=lambda path: path.name)
def test_shared_modules_do_not_embed_football_semantics(module_path: Path) -> None:
    text = module_path.read_text(encoding="utf-8").lower()
    forbidden_tokens = (
        "home_score",
        "away_score",
        "avgh",
        "b365h",
        "accumulator",
        "home_canonical_participant_id",
        "competition_id:",
    )
    if module_path.name == "artifacts.py":
        assert "competition_id" not in text
        assert "team-level historical football" not in text
    if module_path.name == "contracts.py":
        for token in forbidden_tokens:
            assert token not in text


def _write_raw_model(tmp_path: Path, *, document: dict) -> None:
    models_root = tmp_path / "models"
    model_dir = models_root / "bad"
    model_dir.mkdir(parents=True)
    text = dumps_canonical_json(document) + "\n"
    (model_dir / "model.json").write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (model_dir / MODEL_CHECKSUM_SIDECAR).write_text(f"{checksum}\n", encoding="utf-8", newline="\n")


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


def _manifest_path(tmp_path: Path) -> str:
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(
        tmp_path,
        snapshot_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        snapshots_directory=snapshots_directory,
        season_label="2023-2024",
        source_season_code="2324",
        content=synthetic_season_csv(
            season_start_year=2023,
            match_count=30,
            include_closing_avg=True,
        ),
    )
    database = database_path(tmp_path / "db")
    service = publication_service(database, snapshots_directory)
    return service.publish_or_reuse(
        prepared,
        actor="test",
        correlation_id="job",
    ).snapshot_relative_path


def _built_artifact(tmp_path: Path):
    paths = _paths(tmp_path)
    manifest = _manifest_path(tmp_path)
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
    return artifact


def _loadable_artifact(tmp_path: Path):
    paths = _paths(tmp_path)
    artifact = _built_artifact(tmp_path)
    return paths, artifact


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

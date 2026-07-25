"""Training service coordinating feature build and football 1X2 model fitting."""

from __future__ import annotations

from dataclasses import dataclass

from sports_analytics.core.exceptions import FeatureError, TrainingError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.core.runtime import seed_deterministic_generators
from sports_analytics.data.types import JsonValue
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.football.datasets import (
    BuiltFeatureArtifact,
    build_feature_artifact,
    load_feature_artifact,
    snapshot_feature_artifact_bytes,
)
from sports_analytics.models.artifacts import (
    FeatureArtifactLineage,
    ModelArtifact,
    infer_calibrated_probabilities,
    load_model_artifact,
)
from sports_analytics.models.football_1x2 import (
    Football1x2TrainingResult,
    football_1x2_logistic_specification,
    train_final_artifact,
)
from sports_analytics.snapshots.parquet import file_sha256_and_size


@dataclass(frozen=True, slots=True)
class FeatureBuildRequest:
    """Explicit request to build a football 1X2 feature artifact."""

    relative_manifest_paths: tuple[str, ...]
    minimum_events: int = 30
    split_config: TemporalSplitConfig | None = None
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrainRequest:
    """Explicit request to train from a feature artifact."""

    feature_relative_directory: str
    feature_manifest_checksum: str | None = None
    random_seed: int = 42
    artifact_id: str | None = None


def build_football_1x2_features(
    *,
    paths: RuntimePaths,
    request: FeatureBuildRequest,
) -> BuiltFeatureArtifact:
    """Build an immutable football 1X2 feature artifact from explicit snapshots."""
    if not request.relative_manifest_paths:
        msg = "at least one snapshot manifest path is required"
        raise TrainingError(msg)
    try:
        return build_feature_artifact(
            features_root=paths.features_directory,
            snapshots_directory=paths.snapshots_directory,
            relative_manifest_paths=request.relative_manifest_paths,
            split_config=request.split_config,
            artifact_id=request.artifact_id,
            minimum_events=request.minimum_events,
        )
    except FeatureError as exc:
        raise TrainingError(str(exc)) from exc


def train_football_1x2_model(
    *,
    paths: RuntimePaths,
    request: TrainRequest,
) -> Football1x2TrainingResult:
    """Train, calibrate, evaluate, and persist a football 1X2 baseline model."""
    seed_deterministic_generators(request.random_seed)
    try:
        manifest, vectors, quotes, folds = load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=request.feature_relative_directory,
            expected_manifest_checksum=request.feature_manifest_checksum,
        )
    except FeatureError as exc:
        raise TrainingError(str(exc)) from exc

    feature_directory = paths.features_directory / request.feature_relative_directory.replace(
        "\\", "/"
    )
    feature_bytes_before = snapshot_feature_artifact_bytes(feature_directory)

    split_payload = manifest.get("fold_configuration")
    if not isinstance(split_payload, dict):
        msg = "feature artifact is missing fold_configuration"
        raise TrainingError(msg)
    split = TemporalSplitConfig(
        min_train_rows=int(split_payload["min_train_rows"]),
        min_calibration_rows=int(split_payload["min_calibration_rows"]),
        min_test_rows=int(split_payload["min_test_rows"]),
        step_rows=int(split_payload["step_rows"]),
        maximum_folds=int(split_payload["maximum_folds"]),
    )
    relative_feature_dir = request.feature_relative_directory.replace("\\", "/")
    feature_directory = paths.features_directory / relative_feature_dir
    folds_digest, _ = file_sha256_and_size(feature_directory / "folds.parquet")
    checksum_path = feature_directory / "manifest_checksum.sha256"
    feature_lineage = FeatureArtifactLineage(
        feature_artifact_id=str(manifest["artifact_id"]),
        feature_manifest_path=f"{relative_feature_dir}/manifest.json",
        feature_manifest_checksum_sha256=checksum_path.read_text(encoding="utf-8").strip(),
        feature_specification_version=str(manifest["feature_specification_version"]),
        fold_configuration=dict(split_payload),
        folds_file_checksum_sha256=folds_digest,
        input_snapshots=[dict(item) for item in (manifest.get("input_snapshots") or [])],
    )
    competition_id = str(manifest["competition_id"])
    result = train_final_artifact(
        vectors=vectors,
        folds=folds,
        closing_quotes=quotes,
        feature_lineage=feature_lineage,
        competition_id=competition_id,
        models_root=paths.models_directory,
        random_seed=request.random_seed,
        split_config=split,
        artifact_id=request.artifact_id,
    )
    feature_bytes_after = snapshot_feature_artifact_bytes(feature_directory)
    if feature_bytes_before != feature_bytes_after:
        msg = "training modified the feature artifact directory"
        raise TrainingError(msg)
    return result


def verify_model_artifact(
    *,
    paths: RuntimePaths,
    relative_path: str,
    expected_checksum: str | None = None,
) -> ModelArtifact:
    """Load and verify a model artifact."""
    specification = football_1x2_logistic_specification("football-1x2-prematch-features-v1")
    return load_model_artifact(
        models_root=paths.models_directory,
        relative_path=relative_path,
        specification=specification,
        expected_checksum=expected_checksum,
    )


def infer_from_feature_row(
    *,
    paths: RuntimePaths,
    model_relative_path: str,
    feature_names: tuple[str, ...],
    feature_values: tuple[float, ...],
    feature_specification_version: str,
    expected_checksum: str | None = None,
) -> dict[str, float]:
    """Run calibrated inference for one validated feature row."""
    specification = football_1x2_logistic_specification(feature_specification_version)
    artifact = load_model_artifact(
        models_root=paths.models_directory,
        relative_path=model_relative_path,
        specification=specification,
        expected_checksum=expected_checksum,
    )
    return infer_calibrated_probabilities(
        artifact=artifact,
        feature_names=feature_names,
        feature_values=feature_values,
        feature_specification_version=feature_specification_version,
    )


def snapshots_to_json(artifact: BuiltFeatureArtifact) -> list[dict[str, JsonValue]]:
    """Serialize snapshot identities for logging."""
    return [
        {
            "snapshot_id": item.snapshot_id,
            "relative_manifest_path": item.relative_manifest_path,
            "manifest_checksum_sha256": item.manifest_checksum_sha256,
            "season_id": f"{item.scope_id}:{item.partition_label}",
        }
        for item in artifact.snapshot_identities
    ]

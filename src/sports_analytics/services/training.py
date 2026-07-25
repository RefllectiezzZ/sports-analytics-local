"""Training service coordinating feature build and football 1X2 model fitting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sports_analytics.core.exceptions import FeatureError, TrainingError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.core.runtime import seed_deterministic_generators
from sports_analytics.data.types import JsonValue
from sports_analytics.evaluation.temporal import TemporalSplitConfig, assign_fold_rows
from sports_analytics.features.football.datasets import (
    BuiltFeatureArtifact,
    build_feature_artifact,
    load_feature_artifact,
    write_folds_parquet,
)
from sports_analytics.models.artifacts import (
    ModelArtifact,
    infer_calibrated_probabilities,
    load_model_artifact,
)
from sports_analytics.models.football_1x2 import (
    Football1x2TrainingResult,
    prepare_folds,
    train_final_artifact,
)


@dataclass(frozen=True, slots=True)
class FeatureBuildRequest:
    """Explicit request to build a football 1X2 feature artifact."""

    relative_manifest_paths: tuple[str, ...]
    minimum_events: int = 30
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrainRequest:
    """Explicit request to train from a feature artifact."""

    feature_relative_directory: str
    feature_manifest_checksum: str | None = None
    random_seed: int = 42
    split_config: TemporalSplitConfig | None = None
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
        manifest, vectors, quotes = load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=request.feature_relative_directory,
            expected_manifest_checksum=request.feature_manifest_checksum,
        )
    except FeatureError as exc:
        raise TrainingError(str(exc)) from exc

    split = request.split_config or TemporalSplitConfig()
    folds = prepare_folds(vectors, config=split)
    feature_directory = Path(paths.features_directory) / request.feature_relative_directory.replace(
        "\\", "/"
    )
    write_folds_parquet(feature_directory, fold_rows=assign_fold_rows(vectors, folds))
    input_snapshots = list(manifest.get("input_snapshots") or [])
    competition_id = str(manifest["competition_id"])
    return train_final_artifact(
        vectors=vectors,
        folds=folds,
        closing_quotes=quotes,
        input_snapshots=input_snapshots,
        competition_id=competition_id,
        models_root=paths.models_directory,
        random_seed=request.random_seed,
        split_config=split,
        artifact_id=request.artifact_id,
    )


def verify_model_artifact(
    *,
    paths: RuntimePaths,
    relative_path: str,
    expected_checksum: str | None = None,
) -> ModelArtifact:
    """Load and verify a model artifact."""
    return load_model_artifact(
        models_root=paths.models_directory,
        relative_path=relative_path,
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
    artifact = load_model_artifact(
        models_root=paths.models_directory,
        relative_path=model_relative_path,
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
            "season_id": item.season_id,
        }
        for item in artifact.snapshot_identities
    ]

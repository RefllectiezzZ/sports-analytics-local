"""Trusted prediction generation with verified model and feature artifact lineage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import FeatureError, PredictionError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.types import validate_sha256_checksum
from sports_analytics.features.football.datasets import load_feature_artifact
from sports_analytics.features.football.specification import FOOTBALL_1X2_FEATURE_NAMES_V1
from sports_analytics.models.artifacts import (
    ModelArtifact,
    infer_calibrated_probabilities,
    load_model_artifact,
)
from sports_analytics.models.football_1x2 import football_1x2_logistic_specification
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    MarketPrediction,
    PredictionInputSnapshot,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.predictions.football import VerifiedFeatureRow, _model_input_snapshots
from sports_analytics.predictions.provenance import (
    PredictionProvenance,
)
from sports_analytics.predictions.replay import derive_historical_replay_cutoff_utc
from sports_analytics.sports.football.markets import match_result_1x2_selection


@dataclass(frozen=True, slots=True)
class VerifiedPredictionRequest:
    """Explicit inputs required for trusted football 1X2 prediction generation."""

    model_relative_path: str
    model_checksum_sha256: str
    feature_relative_directory: str
    feature_manifest_checksum_sha256: str
    canonical_event_id: str
    event_start_utc: datetime
    predicted_at_utc: datetime
    provenance: PredictionProvenance = PredictionProvenance.HISTORICAL_REPLAY


def generate_verified_football_1x2_prediction(
    *,
    paths: RuntimePaths,
    request: VerifiedPredictionRequest,
) -> MarketPrediction:
    """Load explicit artifacts, verify checksums, and infer a complete prediction record."""
    if request.provenance is not PredictionProvenance.HISTORICAL_REPLAY:
        raise PredictionError("only historical-replay provenance is supported in PR #8")
    if not request.model_relative_path or not request.feature_relative_directory:
        raise PredictionError("model and feature artifact paths must be explicit")
    if not request.canonical_event_id:
        raise PredictionError("canonical_event_id must be explicit")
    validate_sha256_checksum(request.model_checksum_sha256)
    validate_sha256_checksum(request.feature_manifest_checksum_sha256)
    specification = football_1x2_logistic_specification("football-1x2-prematch-features-v1")
    artifact = load_model_artifact(
        models_root=paths.models_directory,
        relative_path=request.model_relative_path,
        specification=specification,
        expected_checksum=request.model_checksum_sha256,
    )
    try:
        manifest, vectors, _quotes, _folds = load_feature_artifact(
            features_root=paths.features_directory,
            relative_directory=request.feature_relative_directory,
            expected_manifest_checksum=request.feature_manifest_checksum_sha256,
        )
    except FeatureError as exc:
        raise PredictionError(str(exc)) from exc
    feature_directory = paths.features_directory / request.feature_relative_directory.replace(
        "\\", "/"
    )
    checksum_path = feature_directory / "manifest_checksum.sha256"
    if not checksum_path.is_file():
        raise PredictionError("feature artifact checksum sidecar is missing")
    manifest_checksum = checksum_path.read_text(encoding="utf-8").strip()
    if manifest_checksum != request.feature_manifest_checksum_sha256:
        raise PredictionError("feature manifest checksum does not match request")
    vector = next(
        (
            item
            for item in vectors
            if item.metadata.canonical_event_id == request.canonical_event_id
        ),
        None,
    )
    if vector is None:
        raise PredictionError(
            f"canonical_event_id is absent from feature artifact: {request.canonical_event_id}"
        )
    scheduled_start = vector.metadata.scheduled_start_utc
    if scheduled_start is None:
        raise PredictionError("historical replay requires persisted scheduled_start_utc")
    if request.event_start_utc != scheduled_start:
        raise PredictionError("event_start_utc must equal persisted scheduled_start_utc")
    replay_cutoff = derive_historical_replay_cutoff_utc(scheduled_start)
    if request.predicted_at_utc != replay_cutoff:
        raise PredictionError("predicted_at_utc must equal the derived historical replay cutoff")
    event_date = vector.metadata.event_date
    if artifact.calibrated_through_date >= event_date:
        raise PredictionError("model calibration history reaches replay event date")
    feature_available_at = replay_cutoff
    input_snapshots = _aligned_input_snapshots(
        artifact=artifact,
        manifest_snapshots=_feature_input_snapshots(manifest.get("input_snapshots")),
    )
    feature_row = VerifiedFeatureRow(
        canonical_event_id=request.canonical_event_id,
        feature_artifact_id=str(manifest["artifact_id"]),
        feature_manifest_checksum_sha256=manifest_checksum,
        feature_specification_version=str(manifest["feature_specification_version"]),
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        feature_values=tuple(vector.features[name] for name in FOOTBALL_1X2_FEATURE_NAMES_V1),
        available_at_utc=feature_available_at,
        input_snapshots=input_snapshots,
        artifact_verified=True,
        sufficient_history=True,
        data_quality_passed=False,
    )
    return _historical_prediction(
        artifact=artifact,
        feature_row=feature_row,
        event_start_utc=scheduled_start,
        predicted_at_utc=replay_cutoff,
        feature_available_at_utc=replay_cutoff,
        provenance=PredictionProvenance.HISTORICAL_REPLAY,
    )


def _historical_prediction(
    *,
    artifact: ModelArtifact,
    feature_row: VerifiedFeatureRow,
    event_start_utc: datetime,
    predicted_at_utc: datetime,
    feature_available_at_utc: datetime,
    provenance: PredictionProvenance,
) -> MarketPrediction:
    """Generate a verified but explicitly non-production-eligible historical prediction."""
    if feature_available_at_utc > predicted_at_utc:
        raise PredictionError("feature data was not available at prediction time")
    if predicted_at_utc >= event_start_utc:
        raise PredictionError("prediction time must be strictly before event start")
    model_lineage = artifact.feature_lineage
    if feature_row.feature_artifact_id != model_lineage.feature_artifact_id:
        raise PredictionError("feature artifact id does not match model lineage")
    if (
        feature_row.feature_manifest_checksum_sha256
        != model_lineage.feature_manifest_checksum_sha256
    ):
        raise PredictionError("feature manifest checksum does not match model lineage")
    if feature_row.feature_specification_version != model_lineage.feature_specification_version:
        raise PredictionError("feature specification does not match model lineage")
    input_snapshots = _model_input_snapshots(model_lineage.input_snapshots)
    if feature_row.input_snapshots and feature_row.input_snapshots != input_snapshots:
        raise PredictionError("feature row input snapshots do not match model lineage")
    probabilities = infer_calibrated_probabilities(
        artifact=artifact,
        feature_names=feature_row.feature_names,
        feature_values=feature_row.feature_values,
        feature_specification_version=feature_row.feature_specification_version,
    )
    lineage = PredictionLineage(
        model_artifact_id=str(artifact.document["artifact_id"]),
        model_checksum_sha256=artifact.checksum_sha256,
        model_specification_version=artifact.specification.model_specification_version,
        feature_artifact_id=feature_row.feature_artifact_id,
        feature_manifest_checksum_sha256=feature_row.feature_manifest_checksum_sha256,
        feature_specification_version=feature_row.feature_specification_version,
        feature_row_id=feature_row.canonical_event_id,
        trained_through_date=artifact.trained_through_date,
        calibrated_through_date=artifact.calibrated_through_date,
        input_snapshots=input_snapshots,
    )
    selection_probabilities = tuple(
        SelectionProbability(
            selection=CanonicalSelectionIdentity.from_selection(
                match_result_1x2_selection(outcome_key)
            ),
            probability=probability,
        )
        for outcome_key in artifact.specification.outcome_space.ordered_labels
        for probability in (probabilities[outcome_key],)
    )
    return build_market_prediction(
        canonical_event_id=feature_row.canonical_event_id,
        event_start_utc=event_start_utc,
        predicted_at_utc=predicted_at_utc,
        feature_available_at_utc=feature_available_at_utc,
        lineage=lineage,
        probabilities=selection_probabilities,
        ordered_selection_space=tuple(item.selection for item in selection_probabilities),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=False,
        ),
        provenance=provenance,
    )


def _aligned_input_snapshots(
    *,
    artifact: ModelArtifact,
    manifest_snapshots: tuple[PredictionInputSnapshot, ...],
) -> tuple[PredictionInputSnapshot, ...]:
    model_snapshots = _model_input_snapshots(artifact.feature_lineage.input_snapshots)
    if model_snapshots:
        if manifest_snapshots and manifest_snapshots != model_snapshots:
            raise PredictionError("feature manifest snapshots do not match model artifact")
        return model_snapshots
    return manifest_snapshots


def _feature_input_snapshots(raw: object) -> tuple[PredictionInputSnapshot, ...]:
    if not isinstance(raw, list):
        return ()
    snapshots: list[PredictionInputSnapshot] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PredictionError("feature input snapshot lineage is malformed")
        snapshot_id = item.get("snapshot_id")
        checksum = item.get("manifest_checksum_sha256")
        schema = item.get("schema_version")
        source = item.get("source_name")
        if type(snapshot_id) is not str or type(checksum) is not str:
            raise PredictionError("feature input snapshot lineage is incomplete")
        manifest_path = item.get("relative_manifest_path")
        source_name = (
            source
            if type(source) is str and source
            else (
                Path(str(manifest_path)).parts[0]
                if type(manifest_path) is str and Path(manifest_path).parts
                else "unknown-source"
            )
        )
        snapshots.append(
            PredictionInputSnapshot(
                snapshot_id=snapshot_id,
                manifest_checksum_sha256=checksum,
                schema_version=schema if type(schema) is str else "snapshot-manifest-v1",
                source_name=source_name,
            )
        )
    return tuple(sorted(snapshots, key=lambda value: value.snapshot_id))

"""Production football full-match 1X2 prediction adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from sports_analytics.core.exceptions import PredictionError
from sports_analytics.data.types import JsonValue
from sports_analytics.features.football.metadata import FOOTBALL_FORBIDDEN_MODEL_FEATURE_FIELDS
from sports_analytics.features.football.specification import FOOTBALL_1X2_FEATURE_NAMES_V1
from sports_analytics.models.artifacts import ModelArtifact, infer_calibrated_probabilities
from sports_analytics.predictions.contracts import (
    CanonicalSelectionIdentity,
    MarketPrediction,
    PredictionInputSnapshot,
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.sports.football.markets import match_result_1x2_selection


@dataclass(frozen=True, slots=True)
class VerifiedFeatureRow:
    """One immutable feature vector with verified artifact provenance."""

    canonical_event_id: str
    feature_artifact_id: str
    feature_manifest_checksum_sha256: str
    feature_specification_version: str
    feature_names: tuple[str, ...]
    feature_values: tuple[float, ...]
    available_at_utc: datetime
    input_snapshots: tuple[PredictionInputSnapshot, ...] = ()
    artifact_verified: bool = True
    sufficient_history: bool = True
    data_quality_passed: bool = True

    def __post_init__(self) -> None:
        if type(self.artifact_verified) is not bool:
            raise PredictionError("artifact_verified must be boolean")
        if type(self.sufficient_history) is not bool:
            raise PredictionError("sufficient_history must be boolean")
        if type(self.data_quality_passed) is not bool:
            raise PredictionError("data_quality_passed must be boolean")
        forbidden = set(self.feature_names) & set(FOOTBALL_FORBIDDEN_MODEL_FEATURE_FIELDS)
        post_event_patterns = ("result", "score", "goals", "outcome", "target", "settled")
        suspicious = {
            name
            for name in self.feature_names
            if any(token in name.casefold() for token in post_event_patterns)
        }
        if forbidden or suspicious:
            names = ", ".join(sorted(forbidden | suspicious))
            raise PredictionError(f"forbidden target/post-event feature fields: {names}")


def predict_football_1x2(
    *,
    artifact: ModelArtifact,
    feature_row: VerifiedFeatureRow,
    event_start_utc: datetime,
    predicted_at_utc: datetime,
) -> MarketPrediction:
    """Infer a complete football 1X2 prediction after exact lineage verification."""
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
    if feature_row.feature_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        raise PredictionError("football feature names do not match the ordered v1 whitelist")
    if not (
        feature_row.artifact_verified
        and feature_row.sufficient_history
        and feature_row.data_quality_passed
    ):
        raise PredictionError("football production prediction requires all quality flags")
    input_snapshots = _model_input_snapshots(artifact.feature_lineage.input_snapshots)
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
    ordered_selection_space = tuple(item.selection for item in selection_probabilities)
    return build_market_prediction(
        canonical_event_id=feature_row.canonical_event_id,
        event_start_utc=event_start_utc,
        predicted_at_utc=predicted_at_utc,
        feature_available_at_utc=feature_row.available_at_utc,
        lineage=lineage,
        probabilities=selection_probabilities,
        ordered_selection_space=ordered_selection_space,
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=feature_row.artifact_verified,
            sufficient_history=feature_row.sufficient_history,
            data_quality_passed=feature_row.data_quality_passed,
        ),
    )


def _model_input_snapshots(
    raw_snapshots: list[dict[str, JsonValue]],
) -> tuple[PredictionInputSnapshot, ...]:
    snapshots: list[PredictionInputSnapshot] = []
    for raw in raw_snapshots:
        snapshot_id = raw.get("snapshot_id")
        checksum = raw.get("manifest_checksum_sha256")
        if type(snapshot_id) is not str or type(checksum) is not str:
            raise PredictionError("model input snapshot lineage is incomplete")
        raw_path = raw.get("relative_manifest_path")
        source = raw.get("source_name")
        source_name = (
            source
            if type(source) is str and source
            else (
                PurePosixPath(raw_path).parts[0]
                if type(raw_path) is str and PurePosixPath(raw_path).parts
                else "unknown-source"
            )
        )
        raw_schema = raw.get("schema_version")
        schema_version = raw_schema if type(raw_schema) is str else "snapshot-manifest-v1"
        snapshots.append(
            PredictionInputSnapshot(
                snapshot_id=snapshot_id,
                manifest_checksum_sha256=checksum,
                schema_version=schema_version,
                source_name=source_name,
            )
        )
    return tuple(sorted(snapshots, key=lambda item: item.snapshot_id))

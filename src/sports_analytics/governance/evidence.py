"""Construction of governance evidence from verified typed backtest artifacts."""

from __future__ import annotations

from typing import cast

from sports_analytics.artifact_strict import (
    require_canonical_utc_timestamp_string,
    require_dict,
    require_finite_number,
    require_int,
    require_sha256_checksum,
    require_str,
)
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.core.exceptions import GovernanceError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.types import JsonValue
from sports_analytics.governance.contracts import ModelEvaluationEvidence, ModelRegistryEntry
from sports_analytics.services.training import verify_model_artifact


def build_model_evaluation_evidence(
    *,
    paths: RuntimePaths,
    registry_entry: ModelRegistryEntry,
    payload: object,
) -> ModelEvaluationEvidence:
    """Return evidence derived only from a strict typed backtest artifact.

    The request identifies a persisted artifact and its aggregate metric row;
    values, population, window, model identity, and scope are all read back
    from verified rows instead of being caller assertions.
    """
    raw = require_dict(payload, field="evaluation evidence reference")
    if set(raw) != {
        "relative_directory",
        "checksum_sha256",
        "artifact_id",
        "schema_version",
        "metric_id",
    }:
        raise GovernanceError("evaluation evidence reference fields are not exact")
    artifact = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=require_str(raw.get("relative_directory"), field="relative_directory"),
        expected_kind="backtest",
        expected_schema_version=require_str(raw.get("schema_version"), field="schema_version"),
        expected_checksum=require_sha256_checksum(
            raw.get("checksum_sha256"), field="checksum_sha256"
        ),
        expected_artifact_id=require_str(raw.get("artifact_id"), field="artifact_id"),
    )
    # Re-load the registered model from its contained relative location.  This
    # couples the DB registry to the actual immutable model bytes.
    model = verify_model_artifact(
        paths=paths,
        relative_path=registry_entry.model_relative_path,
        expected_checksum=registry_entry.model_checksum_sha256,
    )
    if model.document.get("artifact_id") != registry_entry.model_artifact_id:
        raise GovernanceError("registered model artifact identity no longer verifies")
    aggregate_rows = artifact.dataset("aggregate_metrics").rows
    metric_id = require_str(raw.get("metric_id"), field="metric_id")
    rows = [row for row in aggregate_rows if row.get("metric_id") == metric_id]
    if len(rows) != 1:
        raise GovernanceError("evaluation evidence metric selection is missing or ambiguous")
    aggregate = rows[0]
    opportunities = artifact.dataset("opportunities").rows
    predictions = artifact.dataset("predictions").rows
    if not opportunities or not predictions:
        raise GovernanceError("evaluation artifact has no usable model evidence")
    model_ids = {
        require_str(row.get("model_artifact_id"), field="model_artifact_id")
        for row in opportunities
    }
    model_checksums = {
        require_sha256_checksum(row.get("model_checksum_sha256"), field="model_checksum_sha256")
        for row in opportunities
    }
    if model_ids != {registry_entry.model_artifact_id} or model_checksums != {
        registry_entry.model_checksum_sha256
    }:
        raise GovernanceError("evaluation artifact does not reference the registered model exactly")
    modes = {
        require_str(row.get("evaluation_mode"), field="evaluation_mode") for row in opportunities
    }
    scopes = {
        (
            require_str(
                require_dict(row.get("selection"), field="selection").get("sport_code"),
                field="sport_code",
            ),
            require_str(
                require_dict(row.get("selection"), field="selection").get("market_key"),
                field="market_key",
            ),
        )
        for row in opportunities
    }
    if len(modes) != 1 or scopes != {registry_entry.scope}:
        raise GovernanceError(
            "evaluation artifact scope or mode is incompatible with registered model"
        )
    event_times = [
        require_canonical_utc_timestamp_string(row.get("event_start_utc"), field="event_start_utc")
        for row in opportunities
    ]
    event_ids = sorted(
        require_str(row.get("canonical_event_id"), field="canonical_event_id")
        for row in predictions
    )
    if len(event_ids) != len(set(event_ids)):
        raise GovernanceError("evaluation artifact has duplicate prediction event evidence")
    sample_size = require_int(aggregate.get("all_prediction_count"), field="all_prediction_count")
    if sample_size != len(predictions):
        raise GovernanceError("aggregate sample size does not match verified prediction rows")
    log_loss = require_finite_number(aggregate.get("all_log_loss"), field="all_log_loss")
    brier = require_finite_number(
        aggregate.get("all_multiclass_brier_score"), field="all_multiclass_brier_score"
    )
    from sports_analytics.models.identity import content_addressed_id

    return ModelEvaluationEvidence(
        evidence_artifact_id=artifact.artifact_id,
        evidence_checksum_sha256=artifact.checksum_sha256,
        model_artifact_id=registry_entry.model_artifact_id,
        sport_code=registry_entry.sport_code,
        market_key=registry_entry.market_key,
        evaluation_mode=next(iter(modes)),
        window_start_utc=min(event_times),
        window_end_utc=max(event_times),
        event_population_id=content_addressed_id(
            identity_type="governance-event-population-v1",
            payload={"event_ids": cast(list[JsonValue], event_ids)},
        ),
        sample_size=sample_size,
        completed_result_count=sample_size,
        coverage=1.0 if sample_size else 0.0,
        log_loss=log_loss,
        multiclass_brier_score=brier,
        calibration_error=None,
        hit_rate=None,
        roi=None,
    )

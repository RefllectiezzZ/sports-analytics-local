"""Safe persistence and loading of explicit model parameters (no pickle/joblib)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from sports_analytics.core.exceptions import ModelError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
)
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.features.contracts import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    validate_feature_vector,
)
from sports_analytics.models.calibration import softmax, validate_probability_matrix
from sports_analytics.models.contracts import (
    FOOTBALL_1X2_LOGISTIC_MODEL_V1,
    MODEL_MANIFEST_VERSION,
    OUTCOME_LABELS_1X2,
)
from sports_analytics.models.logistic import FittedLogisticParameters, logits_from_parameters
from sports_analytics.snapshots.paths import is_absolute_path_text, resolve_under_root

ARTIFACT_FILENAME: str = "model.json"


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Loaded explicit football 1X2 model artifact."""

    relative_path: str
    checksum_sha256: str
    document: dict[str, Any]
    parameters: FittedLogisticParameters
    temperature: float
    feature_specification_version: str
    model_specification_version: str
    trained_through_date: date
    calibrated_through_date: date


def build_model_document(
    *,
    artifact_id: str,
    parameters: FittedLogisticParameters,
    temperature: float,
    feature_specification_version: str,
    model_specification_version: str,
    competition_id: str,
    trained_through_date: date,
    calibrated_through_date: date,
    input_snapshots: list[dict[str, JsonValue]],
    configuration: dict[str, JsonValue],
    validation_metrics: dict[str, JsonValue],
    evaluation_summary: dict[str, JsonValue],
    generated_at: datetime,
    random_seed: int,
) -> dict[str, JsonValue]:
    """Assemble the canonical JSON document for one deployable model artifact."""
    if parameters.outcome_labels != OUTCOME_LABELS_1X2:
        msg = "model artifact outcome labels must be home/draw/away"
        raise ModelError(msg)
    if parameters.feature_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        msg = "model artifact feature names must match the v1 whitelist"
        raise ModelError(msg)
    if temperature <= 0 or not np.isfinite(temperature):
        msg = "calibration temperature must be positive and finite"
        raise ModelError(msg)
    return {
        "manifest_version": MODEL_MANIFEST_VERSION,
        "artifact_id": artifact_id,
        "artifact_type": "football-1x2-logistic-model",
        "model_specification_version": model_specification_version,
        "feature_specification_version": feature_specification_version,
        "sport_code": "football",
        "market_key": "football.match-result.1x2.full-match",
        "competition_id": competition_id,
        "outcome_labels": list(OUTCOME_LABELS_1X2),
        "ordered_feature_names": list(parameters.feature_names),
        "scaler_mean": list(parameters.scaler_mean),
        "scaler_scale": list(parameters.scaler_scale),
        "coefficients": [list(row) for row in parameters.coefficients],
        "intercepts": list(parameters.intercepts),
        "calibration_temperature": float(temperature),
        "trained_through_date": trained_through_date.isoformat(),
        "calibrated_through_date": calibrated_through_date.isoformat(),
        "input_snapshots": ensure_json_value(input_snapshots),
        "configuration": configuration,
        "validation_metrics": validation_metrics,
        "evaluation_summary": evaluation_summary,
        "random_seed": random_seed,
        "generated_at_utc": format_utc_timestamp(generated_at),
        "serialization": {
            "format": "explicit-json-parameters",
            "pickle": False,
            "joblib": False,
            "executes_python": False,
        },
        "limitations": [
            "Team-level historical football 1X2 baseline only.",
            "Not a betting recommendation engine.",
            "Does not use players, injuries, or lineups.",
            "Does not use bookmaker odds as model features.",
            "Does not produce expected value or accumulators.",
            "Past validation performance is not a guarantee of future performance.",
        ],
    }


def write_model_artifact(
    *,
    models_root: Path,
    relative_directory: str,
    document: dict[str, JsonValue],
) -> tuple[Path, str]:
    """Persist ``model.json`` under the models root and return path plus checksum."""
    if is_absolute_path_text(relative_directory):
        msg = "model artifact path must be relative under the models root"
        raise ModelError(msg)
    normalized = relative_directory.replace("\\", "/")
    directory = resolve_under_root(
        models_root,
        normalized,
        expect_file=False,
        error_type=ModelError,
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ARTIFACT_FILENAME
    if path.exists():
        msg = f"model artifact already exists: {normalized}/{ARTIFACT_FILENAME}"
        raise ModelError(msg)
    # Reject accidental pickle/joblib siblings in the same directory.
    for banned in ("model.pkl", "model.joblib", "model.pickle", "pipeline.joblib"):
        if (directory / banned).exists():
            msg = f"unsafe serialized artifact present: {banned}"
            raise ModelError(msg)
    text = dumps_canonical_json(document) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return path, checksum


def load_model_artifact(
    *,
    models_root: Path,
    relative_path: str,
    expected_checksum: str | None = None,
) -> ModelArtifact:
    """Load and verify an explicit model artifact. Never executes Python code."""
    if is_absolute_path_text(relative_path):
        msg = "model artifact path must be relative under the models root"
        raise ModelError(msg)
    normalized = relative_path.replace("\\", "/")
    if normalized.endswith("/"):
        normalized = f"{normalized}{ARTIFACT_FILENAME}"
    if not normalized.endswith(f"/{ARTIFACT_FILENAME}") and normalized != ARTIFACT_FILENAME:
        # Allow directory form.
        candidate = f"{normalized}/{ARTIFACT_FILENAME}"
        normalized = candidate
    path = resolve_under_root(
        models_root,
        normalized,
        expect_file=True,
        error_type=ModelError,
    )
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_checksum is not None:
        expected = validate_sha256_checksum(expected_checksum)
        if digest != expected:
            msg = "model artifact checksum mismatch"
            raise ModelError(msg)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "model artifact JSON is malformed"
        raise ModelError(msg) from exc
    if not isinstance(document, dict):
        msg = "model artifact must be a JSON object"
        raise ModelError(msg)
    parameters = _parameters_from_document(document)
    temperature = float(document["calibration_temperature"])
    if temperature <= 0 or not np.isfinite(temperature):
        msg = "invalid calibration temperature in artifact"
        raise ModelError(msg)
    if document.get("serialization", {}).get("pickle") or document.get("serialization", {}).get(
        "joblib"
    ):
        msg = "refusing to load artifact that claims pickle/joblib serialization"
        raise ModelError(msg)
    parent = path.parent
    for banned in ("model.pkl", "model.joblib", "model.pickle", "pipeline.joblib"):
        if (parent / banned).exists():
            msg = f"refusing to load model directory containing {banned}"
            raise ModelError(msg)
    return ModelArtifact(
        relative_path=normalized,
        checksum_sha256=digest,
        document=document,
        parameters=parameters,
        temperature=temperature,
        feature_specification_version=str(document["feature_specification_version"]),
        model_specification_version=str(document["model_specification_version"]),
        trained_through_date=date.fromisoformat(str(document["trained_through_date"])),
        calibrated_through_date=date.fromisoformat(str(document["calibrated_through_date"])),
    )


def infer_calibrated_probabilities(
    *,
    artifact: ModelArtifact,
    feature_names: tuple[str, ...],
    feature_values: tuple[float, ...],
    feature_specification_version: str,
) -> dict[str, float]:
    """Pure inference from explicit parameters for one feature row."""
    validate_feature_vector(
        feature_names=feature_names,
        values=feature_values,
        expected_specification_version=artifact.feature_specification_version,
        provided_specification_version=feature_specification_version,
    )
    if artifact.model_specification_version != FOOTBALL_1X2_LOGISTIC_MODEL_V1:
        msg = "unsupported model specification version"
        raise ModelError(msg)
    if feature_names != artifact.parameters.feature_names:
        msg = "feature names do not match the model artifact whitelist"
        raise ModelError(msg)
    vector = np.asarray(feature_values, dtype=np.float64)
    logits = logits_from_parameters(feature_vector=vector, parameters=artifact.parameters)
    probs = softmax(logits, temperature=artifact.temperature)[0]
    validate_probability_matrix(probs.reshape(1, -1))
    return {label: float(probs[index]) for index, label in enumerate(OUTCOME_LABELS_1X2)}


def _parameters_from_document(document: dict[str, Any]) -> FittedLogisticParameters:
    try:
        feature_names = tuple(str(item) for item in document["ordered_feature_names"])
        outcome_labels = tuple(str(item) for item in document["outcome_labels"])
        scaler_mean = tuple(float(item) for item in document["scaler_mean"])
        scaler_scale = tuple(float(item) for item in document["scaler_scale"])
        coefficients = tuple(
            tuple(float(value) for value in row) for row in document["coefficients"]
        )
        intercepts = tuple(float(item) for item in document["intercepts"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = "model artifact parameters are malformed"
        raise ModelError(msg) from exc
    if feature_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        msg = "model artifact feature whitelist mismatch"
        raise ModelError(msg)
    if outcome_labels != OUTCOME_LABELS_1X2:
        msg = "model artifact outcome label order mismatch"
        raise ModelError(msg)
    n_features = len(feature_names)
    if len(scaler_mean) != n_features or len(scaler_scale) != n_features:
        msg = "scaler parameter length mismatch"
        raise ModelError(msg)
    if len(coefficients) != 3 or any(len(row) != n_features for row in coefficients):
        msg = "coefficient matrix has invalid shape"
        raise ModelError(msg)
    if len(intercepts) != 3:
        msg = "intercept vector has invalid length"
        raise ModelError(msg)
    if any(scale <= 0 for scale in scaler_scale):
        msg = "scaler scales must be positive"
        raise ModelError(msg)
    return FittedLogisticParameters(
        feature_names=feature_names,
        outcome_labels=outcome_labels,
        scaler_mean=scaler_mean,
        scaler_scale=scaler_scale,
        coefficients=coefficients,
        intercepts=intercepts,
    )

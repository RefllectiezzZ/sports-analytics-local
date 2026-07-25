"""Safe persistence and loading of explicit model parameters (no pickle/joblib)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from sports_analytics.core.exceptions import ModelError
from sports_analytics.data.codec import dumps_canonical_json, ensure_json_value
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.features.contracts import validate_feature_vector
from sports_analytics.models.calibration import softmax
from sports_analytics.models.contracts import (
    MODEL_CHECKSUM_SIDECAR,
    MODEL_MANIFEST_VERSION,
    ModelSpecification,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.models.logistic import (
    FittedLogisticParameters,
    LogisticConfiguration,
    logits_from_parameters,
)
from sports_analytics.snapshots.paths import is_absolute_path_text, resolve_under_root

ARTIFACT_FILENAME: str = "model.json"
MODEL_IDENTITY_VERSION: str = "model-identity-v1"
MODEL_IDENTITY_TYPE: str = "model-artifact"


@dataclass(frozen=True, slots=True)
class FeatureArtifactLineage:
    """Immutable lineage from one feature artifact into a model artifact."""

    feature_artifact_id: str
    feature_manifest_path: str
    feature_manifest_checksum_sha256: str
    feature_specification_version: str
    fold_configuration: dict[str, JsonValue]
    folds_file_checksum_sha256: str
    input_snapshots: list[dict[str, JsonValue]]


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Loaded explicit model artifact."""

    relative_path: str
    checksum_sha256: str
    document: dict[str, Any]
    parameters: FittedLogisticParameters
    temperature: float
    specification: ModelSpecification
    trained_through_date: date
    calibrated_through_date: date
    feature_lineage: FeatureArtifactLineage


def derive_model_artifact_id(
    *,
    specification: ModelSpecification,
    parameters: FittedLogisticParameters,
    temperature: float,
    trained_through_date: date,
    calibrated_through_date: date,
    feature_lineage: FeatureArtifactLineage,
    evaluation_summary: dict[str, JsonValue],
    random_seed: int,
) -> str:
    """Derive content-addressed model identity from fitted parameters and lineage."""
    if parameters.feature_names != specification.ordered_feature_names:
        msg = "model parameters feature names must match the model specification"
        raise ModelError(msg)
    if parameters.outcome_labels != specification.outcome_space.ordered_labels:
        msg = "model parameters outcome labels must match the model specification"
        raise ModelError(msg)
    logistic_config = parameters.configuration
    evaluation_summary_checksum = hashlib.sha256(
        dumps_canonical_json(evaluation_summary).encode("utf-8")
    ).hexdigest()
    payload: dict[str, JsonValue] = {
        "identity_version": MODEL_IDENTITY_VERSION,
        "feature_artifact_id": feature_lineage.feature_artifact_id,
        "feature_manifest_checksum_sha256": feature_lineage.feature_manifest_checksum_sha256,
        "folds_file_checksum_sha256": feature_lineage.folds_file_checksum_sha256,
        "model_specification_version": specification.model_specification_version,
        "feature_specification_version": specification.feature_specification_version,
        "ordered_feature_names": list(parameters.feature_names),
        "ordered_outcome_labels": list(parameters.outcome_labels),
        "logistic_configuration": {
            "configuration_version": logistic_config.configuration_version,
            "solver": logistic_config.solver,
            "penalty": logistic_config.penalty,
            "regularization_strength": logistic_config.regularization_strength,
            "tolerance": logistic_config.tolerance,
            "maximum_iterations": logistic_config.maximum_iterations,
            "fit_intercept": logistic_config.fit_intercept,
            "random_seed": logistic_config.random_seed,
            "feature_scaler_policy": logistic_config.feature_scaler_policy,
        },
        "scaler_mean": list(parameters.scaler_mean),
        "scaler_scale": list(parameters.scaler_scale),
        "coefficients": [list(row) for row in parameters.coefficients],
        "intercepts": list(parameters.intercepts),
        "calibration_temperature": float(temperature),
        "trained_through_date": trained_through_date.isoformat(),
        "calibrated_through_date": calibrated_through_date.isoformat(),
        "random_seed": random_seed,
        "sklearn_version": parameters.sklearn_version,
        "numpy_version": parameters.numpy_version,
        "convergence_iterations": list(parameters.convergence_iterations),
        "evaluation_summary_checksum_sha256": evaluation_summary_checksum,
    }
    return content_addressed_id(identity_type=MODEL_IDENTITY_TYPE, payload=payload)


def build_model_document(
    *,
    artifact_id: str,
    specification: ModelSpecification,
    parameters: FittedLogisticParameters,
    temperature: float,
    competition_id: str,
    trained_through_date: date,
    calibrated_through_date: date,
    feature_lineage: FeatureArtifactLineage,
    configuration: dict[str, JsonValue],
    validation_metrics: dict[str, JsonValue],
    evaluation_summary: dict[str, JsonValue],
    random_seed: int,
) -> dict[str, JsonValue]:
    """Assemble the canonical JSON document for one deployable model artifact."""
    ordered_labels = specification.outcome_space.ordered_labels
    if parameters.outcome_labels != ordered_labels:
        msg = "model artifact outcome labels must match the model specification"
        raise ModelError(msg)
    if parameters.feature_names != specification.ordered_feature_names:
        msg = "model artifact feature names must match the model specification"
        raise ModelError(msg)
    if temperature <= 0 or not np.isfinite(temperature):
        msg = "calibration temperature must be positive and finite"
        raise ModelError(msg)
    if trained_through_date > calibrated_through_date:
        msg = "trained_through_date must be on or before calibrated_through_date"
        raise ModelError(msg)
    logistic_config = parameters.configuration
    return {
        "manifest_version": MODEL_MANIFEST_VERSION,
        "artifact_id": artifact_id,
        "artifact_type": f"{specification.sport_code}-{specification.market_key}-logistic-model",
        "model_specification_version": specification.model_specification_version,
        "feature_specification_version": specification.feature_specification_version,
        "sport_code": specification.sport_code,
        "market_key": specification.market_key,
        "competition_id": competition_id,
        "outcome_labels": list(ordered_labels),
        "ordered_feature_names": list(parameters.feature_names),
        "scaler_mean": list(parameters.scaler_mean),
        "scaler_scale": list(parameters.scaler_scale),
        "coefficients": [list(row) for row in parameters.coefficients],
        "intercepts": list(parameters.intercepts),
        "calibration_temperature": float(temperature),
        "trained_through_date": trained_through_date.isoformat(),
        "calibrated_through_date": calibrated_through_date.isoformat(),
        "feature_artifact_id": feature_lineage.feature_artifact_id,
        "feature_manifest_path": feature_lineage.feature_manifest_path,
        "feature_manifest_checksum_sha256": feature_lineage.feature_manifest_checksum_sha256,
        "fold_configuration": ensure_json_value(feature_lineage.fold_configuration),
        "folds_file_checksum_sha256": feature_lineage.folds_file_checksum_sha256,
        "input_snapshots": ensure_json_value(feature_lineage.input_snapshots),
        "configuration": configuration,
        "validation_metrics": validation_metrics,
        "evaluation_summary": evaluation_summary,
        "random_seed": random_seed,
        "logistic_configuration": {
            "configuration_version": logistic_config.configuration_version,
            "solver": logistic_config.solver,
            "penalty": logistic_config.penalty,
            "regularization_strength": logistic_config.regularization_strength,
            "tolerance": logistic_config.tolerance,
            "maximum_iterations": logistic_config.maximum_iterations,
            "fit_intercept": logistic_config.fit_intercept,
            "random_seed": logistic_config.random_seed,
            "feature_scaler_policy": logistic_config.feature_scaler_policy,
            "sklearn_version": parameters.sklearn_version,
            "numpy_version": parameters.numpy_version,
            "convergence_iterations": list(parameters.convergence_iterations),
        },
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
    """Persist ``model.json`` and its checksum sidecar under the models root."""
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
    for banned in ("model.pkl", "model.joblib", "model.pickle", "pipeline.joblib"):
        if (directory / banned).exists():
            msg = f"unsafe serialized artifact present: {banned}"
            raise ModelError(msg)
    text = dumps_canonical_json(document) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sidecar = directory / MODEL_CHECKSUM_SIDECAR
    sidecar.write_text(f"{checksum}\n", encoding="utf-8", newline="\n")
    return path, checksum


def load_model_artifact(
    *,
    models_root: Path,
    relative_path: str,
    specification: ModelSpecification,
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
        normalized = f"{normalized}/{ARTIFACT_FILENAME}"
    path = resolve_under_root(
        models_root,
        normalized,
        expect_file=True,
        error_type=ModelError,
    )
    if path.is_symlink():
        msg = "model artifact path must not be a symlink"
        raise ModelError(msg)
    parent = path.parent
    sidecar_path = parent / MODEL_CHECKSUM_SIDECAR
    if not sidecar_path.is_file():
        msg = "model artifact checksum sidecar is missing"
        raise ModelError(msg)
    sidecar_digest = sidecar_path.read_text(encoding="utf-8").strip()
    validate_sha256_checksum(sidecar_digest)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != sidecar_digest:
        msg = "model artifact checksum sidecar mismatch"
        raise ModelError(msg)
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
    if document.get("manifest_version") != MODEL_MANIFEST_VERSION:
        msg = "unsupported model manifest version"
        raise ModelError(msg)
    if document.get("model_specification_version") != specification.model_specification_version:
        msg = "model specification version mismatch"
        raise ModelError(msg)
    if document.get("feature_specification_version") != specification.feature_specification_version:
        msg = "feature specification version mismatch"
        raise ModelError(msg)
    if document.get("sport_code") != specification.sport_code:
        msg = "sport code mismatch in model artifact"
        raise ModelError(msg)
    if document.get("market_key") != specification.market_key:
        msg = "market key mismatch in model artifact"
        raise ModelError(msg)
    parameters = _parameters_from_document(document, specification=specification)
    temperature = _parse_positive_finite_float(document, "calibration_temperature")
    serialization = document.get("serialization")
    if not isinstance(serialization, dict):
        msg = "model artifact serialization metadata is malformed"
        raise ModelError(msg)
    if serialization.get("pickle") or serialization.get("joblib"):
        msg = "refusing to load artifact that claims pickle/joblib serialization"
        raise ModelError(msg)
    for banned in ("model.pkl", "model.joblib", "model.pickle", "pipeline.joblib"):
        if (parent / banned).exists():
            msg = f"refusing to load model directory containing {banned}"
            raise ModelError(msg)
    trained_through = _parse_date(document, "trained_through_date")
    calibrated_through = _parse_date(document, "calibrated_through_date")
    if trained_through > calibrated_through:
        msg = "trained_through_date must be on or before calibrated_through_date"
        raise ModelError(msg)
    feature_lineage = _feature_lineage_from_document(document)
    try:
        evaluation_summary_raw = document["evaluation_summary"]
        random_seed = int(document["random_seed"])
        if not isinstance(evaluation_summary_raw, dict):
            raise TypeError
        evaluation_summary = ensure_json_value(evaluation_summary_raw)
        if not isinstance(evaluation_summary, dict):
            raise TypeError
    except (KeyError, TypeError, ValueError) as exc:
        msg = "model artifact evaluation identity fields are malformed"
        raise ModelError(msg) from exc
    expected_id = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=temperature,
        trained_through_date=trained_through,
        calibrated_through_date=calibrated_through,
        feature_lineage=feature_lineage,
        evaluation_summary=evaluation_summary,
        random_seed=random_seed,
    )
    if str(document.get("artifact_id")) != expected_id:
        msg = "model artifact_id does not match content-addressed identity"
        raise ModelError(msg)
    return ModelArtifact(
        relative_path=normalized,
        checksum_sha256=digest,
        document=document,
        parameters=parameters,
        temperature=temperature,
        specification=specification,
        trained_through_date=trained_through,
        calibrated_through_date=calibrated_through,
        feature_lineage=feature_lineage,
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
        expected_names=artifact.parameters.feature_names,
        expected_specification_version=artifact.specification.feature_specification_version,
        provided_specification_version=feature_specification_version,
    )
    if feature_names != artifact.parameters.feature_names:
        msg = "feature names do not match the model artifact whitelist"
        raise ModelError(msg)
    vector = np.asarray(feature_values, dtype=np.float64)
    logits = logits_from_parameters(feature_vector=vector, parameters=artifact.parameters)
    probs = softmax(
        logits,
        outcome_space=artifact.specification.outcome_space,
        temperature=artifact.temperature,
    )[0]
    return {
        label: float(probs[index])
        for index, label in enumerate(artifact.specification.outcome_space.ordered_labels)
    }


def _parameters_from_document(
    document: dict[str, Any],
    *,
    specification: ModelSpecification,
) -> FittedLogisticParameters:
    try:
        feature_names = tuple(str(item) for item in document["ordered_feature_names"])
        outcome_labels = tuple(str(item) for item in document["outcome_labels"])
        scaler_mean = tuple(float(item) for item in document["scaler_mean"])
        scaler_scale = tuple(float(item) for item in document["scaler_scale"])
        coefficients = tuple(
            tuple(float(value) for value in row) for row in document["coefficients"]
        )
        intercepts = tuple(float(item) for item in document["intercepts"])
        logistic_payload = document["logistic_configuration"]
        configuration = LogisticConfiguration(
            configuration_version=str(logistic_payload["configuration_version"]),
            solver=str(logistic_payload["solver"]),
            penalty=str(logistic_payload["penalty"]),
            regularization_strength=float(logistic_payload["regularization_strength"]),
            tolerance=float(logistic_payload["tolerance"]),
            maximum_iterations=int(logistic_payload["maximum_iterations"]),
            fit_intercept=bool(logistic_payload["fit_intercept"]),
            random_seed=int(logistic_payload["random_seed"]),
            feature_scaler_policy=str(logistic_payload["feature_scaler_policy"]),
        )
        sklearn_version = str(logistic_payload["sklearn_version"])
        numpy_version = str(logistic_payload["numpy_version"])
        convergence_iterations = tuple(
            int(item) for item in logistic_payload["convergence_iterations"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = "model artifact parameters are malformed"
        raise ModelError(msg) from exc
    try:
        configuration.validate()
    except ModelError:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = "model artifact logistic configuration is invalid"
        raise ModelError(msg) from exc
    ordered_labels = specification.outcome_space.ordered_labels
    if outcome_labels != ordered_labels:
        msg = "model artifact outcome label order mismatch"
        raise ModelError(msg)
    if feature_names != specification.ordered_feature_names:
        msg = "model artifact feature whitelist mismatch"
        raise ModelError(msg)
    n_features = len(feature_names)
    n_outcomes = len(ordered_labels)
    if len(scaler_mean) != n_features or len(scaler_scale) != n_features:
        msg = "scaler parameter length mismatch"
        raise ModelError(msg)
    if len(coefficients) != n_outcomes or any(len(row) != n_features for row in coefficients):
        msg = "coefficient matrix has invalid shape"
        raise ModelError(msg)
    if len(intercepts) != n_outcomes:
        msg = "intercept vector has invalid length"
        raise ModelError(msg)
    if any(scale <= 0 for scale in scaler_scale):
        msg = "scaler scales must be positive"
        raise ModelError(msg)
    if (
        not np.isfinite(np.asarray(coefficients)).all()
        or not np.isfinite(np.asarray(intercepts)).all()
    ):
        msg = "model artifact contains non-finite parameters"
        raise ModelError(msg)
    return FittedLogisticParameters(
        feature_names=feature_names,
        outcome_labels=outcome_labels,
        scaler_mean=scaler_mean,
        scaler_scale=scaler_scale,
        coefficients=coefficients,
        intercepts=intercepts,
        configuration=configuration,
        sklearn_version=sklearn_version,
        numpy_version=numpy_version,
        convergence_iterations=convergence_iterations,
    )


def _feature_lineage_from_document(document: dict[str, Any]) -> FeatureArtifactLineage:
    try:
        snapshots = document["input_snapshots"]
        if not isinstance(snapshots, list):
            raise TypeError
        return FeatureArtifactLineage(
            feature_artifact_id=str(document["feature_artifact_id"]),
            feature_manifest_path=str(document["feature_manifest_path"]),
            feature_manifest_checksum_sha256=str(document["feature_manifest_checksum_sha256"]),
            feature_specification_version=str(document["feature_specification_version"]),
            fold_configuration=dict(document["fold_configuration"]),
            folds_file_checksum_sha256=str(document["folds_file_checksum_sha256"]),
            input_snapshots=[dict(item) for item in snapshots],
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = "model artifact feature lineage is malformed"
        raise ModelError(msg) from exc


def _parse_date(document: dict[str, Any], key: str) -> date:
    try:
        return date.fromisoformat(str(document[key]))
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"model artifact field {key} is malformed"
        raise ModelError(msg) from exc


def _parse_positive_finite_float(document: dict[str, Any], key: str) -> float:
    try:
        value = float(document[key])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"model artifact field {key} is malformed"
        raise ModelError(msg) from exc
    if value <= 0 or not np.isfinite(value):
        msg = f"model artifact field {key} must be positive and finite"
        raise ModelError(msg)
    return value

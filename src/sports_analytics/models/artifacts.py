"""Safe persistence and loading of explicit model parameters (no pickle/joblib)."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from sports_analytics.core.exceptions import ModelError, RepositoryError
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
EXPECTED_FOLD_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "min_train_rows",
        "min_calibration_rows",
        "min_test_rows",
        "step_rows",
        "maximum_folds",
    }
)


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
    scope_metadata: dict[str, JsonValue],
    trained_through_date: date,
    calibrated_through_date: date,
    feature_lineage: FeatureArtifactLineage,
    evaluation_summary: dict[str, JsonValue],
    random_seed: int,
) -> str:
    """Derive content-addressed model identity from fitted parameters and lineage."""
    canonical_scope_metadata = _canonical_scope_metadata(scope_metadata)
    validate_feature_artifact_lineage(
        feature_lineage,
        expected_feature_specification_version=specification.feature_specification_version,
    )
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
        "scope_metadata": canonical_scope_metadata,
    }
    return content_addressed_id(identity_type=MODEL_IDENTITY_TYPE, payload=payload)


def build_model_document(
    *,
    artifact_id: str,
    specification: ModelSpecification,
    parameters: FittedLogisticParameters,
    temperature: float,
    scope_metadata: dict[str, JsonValue],
    trained_through_date: date,
    calibrated_through_date: date,
    feature_lineage: FeatureArtifactLineage,
    configuration: dict[str, JsonValue],
    validation_metrics: dict[str, JsonValue],
    evaluation_summary: dict[str, JsonValue],
    random_seed: int,
    limitations: list[str],
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
    canonical_scope_metadata = _canonical_scope_metadata(scope_metadata)
    validate_feature_artifact_lineage(
        feature_lineage,
        expected_feature_specification_version=specification.feature_specification_version,
    )
    logistic_config = parameters.configuration
    return {
        "manifest_version": MODEL_MANIFEST_VERSION,
        "identity_version": MODEL_IDENTITY_VERSION,
        "artifact_id": artifact_id,
        "artifact_type": f"{specification.sport_code}-{specification.market_key}-logistic-model",
        "model_specification_version": specification.model_specification_version,
        "feature_specification_version": specification.feature_specification_version,
        "sport_code": specification.sport_code,
        "market_key": specification.market_key,
        "scope_metadata": canonical_scope_metadata,
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
        "limitations": ensure_json_value(limitations),
    }


def write_model_artifact(
    *,
    models_root: Path,
    relative_directory: str,
    document: dict[str, JsonValue],
    specification: ModelSpecification,
) -> tuple[Path, str]:
    """Atomically persist ``model.json`` and its checksum sidecar under the models root."""
    if is_absolute_path_text(relative_directory):
        msg = "model artifact path must be relative under the models root"
        raise ModelError(msg)
    normalized = relative_directory.replace("\\", "/")
    final_directory = resolve_under_root(
        models_root,
        normalized,
        expect_file=False,
        error_type=ModelError,
    )
    if final_directory.exists() and any(final_directory.iterdir()):
        msg = f"model artifact directory is not empty: {normalized}"
        raise ModelError(msg)
    models_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".model-{str(document.get('artifact_id', 'draft'))[:8]}-",
            dir=str(models_root.resolve()),
        )
    )
    try:
        for banned in ("model.pkl", "model.joblib", "model.pickle", "pipeline.joblib"):
            if (temp_dir / banned).exists():
                msg = f"unsafe serialized artifact present: {banned}"
                raise ModelError(msg)
        path = temp_dir / ARTIFACT_FILENAME
        text = dumps_canonical_json(document) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        sidecar = temp_dir / MODEL_CHECKSUM_SIDECAR
        sidecar.write_text(f"{checksum}\n", encoding="utf-8", newline="\n")
        temp_relative = f"{temp_dir.name}/{ARTIFACT_FILENAME}"
        load_model_artifact(
            models_root=models_root,
            relative_path=temp_relative,
            specification=specification,
            expected_checksum=checksum,
        )
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(final_directory)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return final_directory / ARTIFACT_FILENAME, checksum


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
    sidecar_digest = _read_checksum_sidecar(parent / MODEL_CHECKSUM_SIDECAR)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != sidecar_digest:
        msg = "model artifact checksum sidecar mismatch"
        raise ModelError(msg)
    if expected_checksum is not None:
        try:
            expected = validate_sha256_checksum(expected_checksum)
        except RepositoryError as exc:
            msg = "model artifact checksum is malformed"
            raise ModelError(msg) from exc
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
    if document.get("identity_version") != MODEL_IDENTITY_VERSION:
        msg = "unsupported model identity version"
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
    feature_lineage = _feature_lineage_from_document(
        document,
        expected_feature_specification_version=specification.feature_specification_version,
    )
    scope_metadata = _canonical_scope_metadata(document["scope_metadata"])
    try:
        evaluation_summary_raw = document["evaluation_summary"]
        random_seed = _parse_strict_int(document["random_seed"], "random_seed")
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
        scope_metadata=scope_metadata,
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


def validate_feature_artifact_lineage(
    lineage: FeatureArtifactLineage,
    *,
    expected_feature_specification_version: str,
) -> None:
    """Validate immutable feature-artifact lineage attached to a model artifact."""
    if not lineage.feature_artifact_id.strip():
        msg = "feature artifact id must be non-empty"
        raise ModelError(msg)
    manifest_path = lineage.feature_manifest_path.replace("\\", "/")
    if is_absolute_path_text(manifest_path):
        msg = "feature manifest path must be relative"
        raise ModelError(msg)
    if ".." in Path(manifest_path).parts or manifest_path in {".", ".."}:
        msg = "feature manifest path must not traverse directories"
        raise ModelError(msg)
    if not manifest_path.endswith("manifest.json"):
        msg = "feature manifest path must end with manifest.json"
        raise ModelError(msg)
    try:
        validate_sha256_checksum(lineage.feature_manifest_checksum_sha256)
        validate_sha256_checksum(lineage.folds_file_checksum_sha256)
    except RepositoryError as exc:
        msg = "model artifact lineage checksum is malformed"
        raise ModelError(msg) from exc
    if lineage.feature_specification_version != expected_feature_specification_version:
        msg = "feature specification version mismatch in model lineage"
        raise ModelError(msg)
    if set(lineage.fold_configuration.keys()) != EXPECTED_FOLD_CONFIG_KEYS:
        msg = "fold configuration in model lineage is malformed"
        raise ModelError(msg)
    for key in EXPECTED_FOLD_CONFIG_KEYS:
        value = lineage.fold_configuration[key]
        if type(value) is not int or isinstance(value, bool) or value < 1:
            msg = f"fold configuration field {key} must be a positive integer"
            raise ModelError(msg)
    if not isinstance(lineage.input_snapshots, list):
        msg = "input_snapshots must be a list"
        raise ModelError(msg)
    for snapshot in lineage.input_snapshots:
        if not isinstance(snapshot, dict):
            msg = "input snapshot lineage entries must be mappings"
            raise ModelError(msg)
        for checksum_key in (
            "manifest_checksum_sha256",
            "feature_manifest_checksum_sha256",
            "folds_file_checksum_sha256",
        ):
            checksum_value = snapshot.get(checksum_key)
            if checksum_value is not None:
                try:
                    validate_sha256_checksum(_parse_strict_str(checksum_value, checksum_key))
                except RepositoryError as exc:
                    msg = "input snapshot lineage checksum is malformed"
                    raise ModelError(msg) from exc


def _parameters_from_document(
    document: dict[str, Any],
    *,
    specification: ModelSpecification,
) -> FittedLogisticParameters:
    try:
        feature_names = tuple(
            _parse_strict_str(item, "ordered_feature_names")
            for item in document["ordered_feature_names"]
        )
        outcome_labels = tuple(
            _parse_strict_str(item, "outcome_labels") for item in document["outcome_labels"]
        )
        scaler_mean = _parse_finite_float_sequence(document["scaler_mean"], "scaler_mean")
        scaler_scale = _parse_positive_finite_float_sequence(
            document["scaler_scale"],
            "scaler_scale",
        )
        coefficients = tuple(
            _parse_finite_float_sequence(row, "coefficients") for row in document["coefficients"]
        )
        intercepts = _parse_finite_float_sequence(document["intercepts"], "intercepts")
        logistic_payload = document["logistic_configuration"]
        if not isinstance(logistic_payload, dict):
            raise TypeError
        configuration = LogisticConfiguration(
            configuration_version=_parse_strict_str(
                logistic_payload["configuration_version"],
                "configuration_version",
            ),
            solver=_parse_strict_str(logistic_payload["solver"], "solver"),
            penalty=_parse_strict_str(logistic_payload["penalty"], "penalty"),
            regularization_strength=_parse_positive_finite_float_value(
                logistic_payload["regularization_strength"],
                "regularization_strength",
            ),
            tolerance=_parse_positive_finite_float_value(
                logistic_payload["tolerance"],
                "tolerance",
            ),
            maximum_iterations=_parse_strict_int(
                logistic_payload["maximum_iterations"],
                "maximum_iterations",
            ),
            fit_intercept=_parse_strict_bool(logistic_payload["fit_intercept"], "fit_intercept"),
            random_seed=_parse_strict_int(logistic_payload["random_seed"], "random_seed"),
            feature_scaler_policy=_parse_strict_str(
                logistic_payload["feature_scaler_policy"],
                "feature_scaler_policy",
            ),
        )
        sklearn_version = _parse_strict_str(logistic_payload["sklearn_version"], "sklearn_version")
        numpy_version = _parse_strict_str(logistic_payload["numpy_version"], "numpy_version")
        convergence_iterations = _parse_positive_int_sequence(
            logistic_payload["convergence_iterations"],
            "convergence_iterations",
        )
    except (KeyError, TypeError, ValueError, ModelError) as exc:
        if isinstance(exc, ModelError):
            raise
        msg = "model artifact parameters are malformed"
        raise ModelError(msg) from exc
    try:
        configuration.validate(outcome_count=len(specification.outcome_space.ordered_labels))
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


def _feature_lineage_from_document(
    document: dict[str, Any],
    *,
    expected_feature_specification_version: str,
) -> FeatureArtifactLineage:
    try:
        snapshots = document["input_snapshots"]
        if not isinstance(snapshots, list):
            raise TypeError
        feature_artifact_id = _parse_strict_str(
            document["feature_artifact_id"], "feature_artifact_id"
        )
        if not feature_artifact_id.strip():
            raise ValueError
        feature_manifest_path = _parse_strict_str(
            document["feature_manifest_path"],
            "feature_manifest_path",
        )
        if not feature_manifest_path.strip():
            raise ValueError
        lineage = FeatureArtifactLineage(
            feature_artifact_id=feature_artifact_id,
            feature_manifest_path=feature_manifest_path,
            feature_manifest_checksum_sha256=_parse_strict_str(
                document["feature_manifest_checksum_sha256"],
                "feature_manifest_checksum_sha256",
            ),
            feature_specification_version=_parse_strict_str(
                document["feature_specification_version"],
                "feature_specification_version",
            ),
            fold_configuration=dict(document["fold_configuration"]),
            folds_file_checksum_sha256=_parse_strict_str(
                document["folds_file_checksum_sha256"],
                "folds_file_checksum_sha256",
            ),
            input_snapshots=[dict(item) for item in snapshots],
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = "model artifact feature lineage is malformed"
        raise ModelError(msg) from exc
    validate_feature_artifact_lineage(
        lineage,
        expected_feature_specification_version=expected_feature_specification_version,
    )
    return lineage


def _read_checksum_sidecar(path: Path) -> str:
    if path.is_symlink():
        msg = "checksum sidecar must not be a symlink"
        raise ModelError(msg)
    if not path.is_file():
        msg = "checksum sidecar is missing"
        raise ModelError(msg)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        msg = "checksum sidecar must contain exactly one digest"
        raise ModelError(msg)
    digest = lines[0].strip()
    try:
        validate_sha256_checksum(digest)
    except RepositoryError as exc:
        msg = "checksum sidecar digest is malformed"
        raise ModelError(msg) from exc
    return digest


def _canonical_scope_metadata(scope_metadata: object) -> dict[str, JsonValue]:
    if not isinstance(scope_metadata, dict):
        msg = "scope_metadata must be a JSON object"
        raise ModelError(msg)
    if not scope_metadata:
        msg = "scope_metadata must be non-empty"
        raise ModelError(msg)
    validated: dict[str, JsonValue] = {}
    for key, value in scope_metadata.items():
        if type(key) is not str or not key:
            msg = "scope_metadata keys must be non-empty strings"
            raise ModelError(msg)
        try:
            json_value = ensure_json_value(value)
        except RepositoryError as exc:
            msg = "scope_metadata values must be JSON-compatible"
            raise ModelError(msg) from exc
        validated[key] = json_value
    return json.loads(dumps_canonical_json(validated))  # type: ignore[no-any-return]


def _parse_strict_str(value: object, field: str) -> str:
    if type(value) is not str:
        msg = f"model artifact field {field} must be a JSON string"
        raise ModelError(msg)
    return value


def _parse_date(document: dict[str, Any], key: str) -> date:
    try:
        return _parse_strict_iso_date(document[key], key)
    except KeyError as exc:
        msg = f"model artifact field {key} is malformed"
        raise ModelError(msg) from exc


def _parse_strict_iso_date(value: object, field: str) -> date:
    text = _parse_strict_str(value, field)
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        msg = f"model artifact field {field} must use ISO YYYY-MM-DD format"
        raise ModelError(msg)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        msg = f"model artifact field {field} is malformed"
        raise ModelError(msg) from exc
    if parsed.isoformat() != text:
        msg = f"model artifact field {field} must use ISO YYYY-MM-DD format"
        raise ModelError(msg)
    return parsed


def _parse_positive_finite_float(document: dict[str, Any], key: str) -> float:
    try:
        value = _parse_positive_finite_float_value(document[key], key)
    except KeyError as exc:
        msg = f"model artifact field {key} is malformed"
        raise ModelError(msg) from exc
    return value


def _parse_strict_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        msg = f"model artifact field {field} must be a JSON boolean"
        raise ModelError(msg)
    return value


def _parse_strict_int(value: object, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        msg = f"model artifact field {field} must be a JSON integer"
        raise ModelError(msg)
    return value


def _parse_positive_finite_float_value(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        msg = f"model artifact field {field} must be a JSON number"
        raise ModelError(msg)
    parsed = float(value)
    if parsed <= 0 or not np.isfinite(parsed):
        msg = f"model artifact field {field} must be positive and finite"
        raise ModelError(msg)
    return parsed


def _parse_finite_float_sequence(value: object, field: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        msg = f"model artifact field {field} must be a JSON array"
        raise ModelError(msg)
    parsed: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            msg = f"model artifact field {field} must contain JSON numbers"
            raise ModelError(msg)
        number = float(item)
        if not np.isfinite(number):
            msg = f"model artifact field {field} must contain finite numbers"
            raise ModelError(msg)
        parsed.append(number)
    return tuple(parsed)


def _parse_positive_finite_float_sequence(value: object, field: str) -> tuple[float, ...]:
    sequence = _parse_finite_float_sequence(value, field)
    if any(scale <= 0 for scale in sequence):
        msg = f"model artifact field {field} must contain positive numbers"
        raise ModelError(msg)
    return sequence


def _parse_positive_int_sequence(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        msg = f"model artifact field {field} must be a non-empty JSON array"
        raise ModelError(msg)
    parsed: list[int] = []
    for item in value:
        if type(item) is not int or isinstance(item, bool) or item < 1:
            msg = f"model artifact field {field} must contain positive integers"
            raise ModelError(msg)
        parsed.append(item)
    return tuple(parsed)

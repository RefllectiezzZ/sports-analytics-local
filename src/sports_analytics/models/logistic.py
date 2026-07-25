"""Multinomial logistic regression fit (sklearn) and NumPy inference."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version as package_version

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from sports_analytics.core.exceptions import ModelError
from sports_analytics.features.contracts import OutcomeSpace


@dataclass(frozen=True, slots=True)
class LogisticConfiguration:
    """Explicit, versioned multinomial logistic regression settings."""

    configuration_version: str = "logistic-configuration-v1"
    solver: str = "lbfgs"
    penalty: str = "l2"
    regularization_strength: float = 1.0
    tolerance: float = 1e-4
    maximum_iterations: int = 2000
    fit_intercept: bool = True
    random_seed: int = 42
    feature_scaler_policy: str = "standard-zero-scale-to-one"


@dataclass(frozen=True, slots=True)
class FittedLogisticParameters:
    """Explicit logistic parameters in canonical outcome order."""

    feature_names: tuple[str, ...]
    outcome_labels: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    coefficients: tuple[tuple[float, ...], ...]
    intercepts: tuple[float, ...]
    configuration: LogisticConfiguration
    sklearn_version: str
    numpy_version: str
    convergence_iterations: tuple[int, ...]


def fit_multinomial_logistic(
    *,
    feature_matrix: np.ndarray,
    labels: tuple[str, ...],
    feature_names: tuple[str, ...],
    outcome_space: OutcomeSpace,
    configuration: LogisticConfiguration | None = None,
) -> FittedLogisticParameters:
    """Fit a multinomial logistic model and return explicit parameters."""
    config = configuration or LogisticConfiguration(random_seed=42)
    ordered_labels = outcome_space.ordered_labels
    if feature_matrix.ndim != 2:
        msg = "feature matrix must be 2-dimensional"
        raise ModelError(msg)
    if feature_matrix.shape[0] != len(labels):
        msg = "feature matrix and labels length mismatch"
        raise ModelError(msg)
    if feature_matrix.shape[1] != len(feature_names):
        msg = "feature matrix width does not match feature_names"
        raise ModelError(msg)
    unique = set(labels)
    if unique != set(ordered_labels):
        msg = (
            "training labels must include every outcome in the supplied outcome space; "
            f"expected {ordered_labels}, found {sorted(unique)}"
        )
        raise ModelError(msg)
    if not np.isfinite(feature_matrix).all():
        msg = "feature matrix contains non-finite values"
        raise ModelError(msg)

    scaler = StandardScaler()
    scaled = scaler.fit_transform(feature_matrix)
    if config.feature_scaler_policy == "standard-zero-scale-to-one":
        scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    else:
        msg = f"unsupported feature scaler policy: {config.feature_scaler_policy}"
        raise ModelError(msg)

    model = LogisticRegression(
        solver=config.solver,
        C=config.regularization_strength,
        tol=config.tolerance,
        max_iter=config.maximum_iterations,
        fit_intercept=config.fit_intercept,
        random_state=config.random_seed,
    )
    model.fit(scaled, np.asarray(labels))
    if any(iteration >= config.maximum_iterations for iteration in model.n_iter_):
        msg = "logistic regression did not converge within the configured maximum iterations"
        raise ModelError(msg)
    if not np.isfinite(model.coef_).all() or not np.isfinite(model.intercept_).all():
        msg = "fitted logistic parameters contain non-finite values"
        raise ModelError(msg)

    class_order = tuple(str(item) for item in model.classes_)
    index_by_label = {label: index for index, label in enumerate(class_order)}
    try:
        ordered_indices = [index_by_label[label] for label in ordered_labels]
    except KeyError as exc:
        msg = "fitted model is missing a required outcome class"
        raise ModelError(msg) from exc

    coefficients = tuple(
        tuple(float(value) for value in model.coef_[index]) for index in ordered_indices
    )
    intercepts = tuple(float(model.intercept_[index]) for index in ordered_indices)
    return FittedLogisticParameters(
        feature_names=feature_names,
        outcome_labels=ordered_labels,
        scaler_mean=tuple(float(value) for value in scaler.mean_),
        scaler_scale=tuple(float(value) for value in scale),
        coefficients=coefficients,
        intercepts=intercepts,
        configuration=config,
        sklearn_version=package_version("scikit-learn"),
        numpy_version=np.__version__,
        convergence_iterations=tuple(int(value) for value in model.n_iter_),
    )


def logits_from_parameters(
    *,
    feature_vector: np.ndarray,
    parameters: FittedLogisticParameters,
) -> np.ndarray:
    """Compute raw logits for one or more rows using explicit parameters."""
    matrix = np.asarray(feature_vector, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.shape[1] != len(parameters.feature_names):
        msg = "feature vector width does not match model feature_names"
        raise ModelError(msg)
    if not np.isfinite(matrix).all():
        msg = "feature vector contains non-finite values"
        raise ModelError(msg)
    mean = np.asarray(parameters.scaler_mean, dtype=np.float64)
    scale = np.asarray(parameters.scaler_scale, dtype=np.float64)
    if np.any(scale <= 0) or not np.isfinite(scale).all() or not np.isfinite(mean).all():
        msg = "invalid scaler parameters"
        raise ModelError(msg)
    scaled = (matrix - mean) / scale
    coef = np.asarray(parameters.coefficients, dtype=np.float64)
    intercept = np.asarray(parameters.intercepts, dtype=np.float64)
    if not np.isfinite(coef).all() or not np.isfinite(intercept).all():
        msg = "model coefficients contain non-finite values"
        raise ModelError(msg)
    logits = scaled @ coef.T + intercept
    if not np.isfinite(logits).all():
        msg = "non-finite logits produced during inference"
        raise ModelError(msg)
    return logits

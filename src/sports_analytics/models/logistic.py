"""Multinomial logistic regression fit (sklearn) and NumPy inference."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from sports_analytics.core.exceptions import ModelError
from sports_analytics.models.contracts import OUTCOME_LABELS_1X2


@dataclass(frozen=True, slots=True)
class FittedLogisticParameters:
    """Explicit logistic parameters in canonical outcome order."""

    feature_names: tuple[str, ...]
    outcome_labels: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    coefficients: tuple[tuple[float, ...], ...]
    intercepts: tuple[float, ...]


def fit_multinomial_logistic(
    *,
    feature_matrix: np.ndarray,
    labels: tuple[str, ...],
    feature_names: tuple[str, ...],
    random_seed: int,
) -> FittedLogisticParameters:
    """Fit a multinomial logistic model and return explicit parameters."""
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
    if unique != set(OUTCOME_LABELS_1X2):
        msg = f"training labels must include all outcomes home/draw/away; found {sorted(unique)}"
        raise ModelError(msg)
    if not np.isfinite(feature_matrix).all():
        msg = "feature matrix contains non-finite values"
        raise ModelError(msg)

    scaler = StandardScaler()
    scaled = scaler.fit_transform(feature_matrix)
    if np.any(scaler.scale_ == 0):
        # Replace zero scales with 1.0 so inference stays well-defined.
        scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    else:
        scale = scaler.scale_

    model = LogisticRegression(
        solver="lbfgs",
        max_iter=2000,
        random_state=random_seed,
    )
    model.fit(scaled, np.asarray(labels))
    class_order = tuple(str(item) for item in model.classes_)
    index_by_label = {label: index for index, label in enumerate(class_order)}
    try:
        ordered_indices = [index_by_label[label] for label in OUTCOME_LABELS_1X2]
    except KeyError as exc:
        msg = "fitted model is missing a required outcome class"
        raise ModelError(msg) from exc

    coefficients = tuple(
        tuple(float(value) for value in model.coef_[index]) for index in ordered_indices
    )
    intercepts = tuple(float(model.intercept_[index]) for index in ordered_indices)
    return FittedLogisticParameters(
        feature_names=feature_names,
        outcome_labels=OUTCOME_LABELS_1X2,
        scaler_mean=tuple(float(value) for value in scaler.mean_),
        scaler_scale=tuple(float(value) for value in scale),
        coefficients=coefficients,
        intercepts=intercepts,
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
    logits = scaled @ coef.T + intercept
    if not np.isfinite(logits).all():
        msg = "non-finite logits produced during inference"
        raise ModelError(msg)
    return logits

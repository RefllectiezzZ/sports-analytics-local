"""Deterministic multiclass temperature scaling."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sports_analytics.core.exceptions import ModelError
from sports_analytics.evaluation.metrics import multiclass_log_loss, validate_probability_matrix
from sports_analytics.features.contracts import OutcomeSpace

TEMPERATURE_SEARCH_LOW: float = 0.05
TEMPERATURE_SEARCH_HIGH: float = 10.0
TEMPERATURE_SEARCH_STEPS: int = 200


@dataclass(frozen=True, slots=True)
class TemperatureCalibrationResult:
    """Result of deterministic temperature search on a calibration region."""

    temperature: float
    calibration_log_loss: float
    candidate_count: int


def softmax(
    logits: np.ndarray,
    *,
    outcome_space: OutcomeSpace,
    temperature: float = 1.0,
) -> np.ndarray:
    """Apply temperature-scaled softmax with finite checks."""
    if temperature <= 0 or not np.isfinite(temperature):
        msg = f"temperature must be a positive finite scalar, got {temperature}"
        raise ModelError(msg)
    array = np.asarray(logits, dtype=np.float64)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.shape[1] != len(outcome_space.ordered_labels):
        msg = "logits width does not match the supplied outcome space"
        raise ModelError(msg)
    if not np.isfinite(array).all():
        msg = "logits contain non-finite values"
        raise ModelError(msg)
    scaled = array / temperature
    shifted = scaled - np.max(scaled, axis=1, keepdims=True)
    exp = np.exp(shifted)
    sums = np.sum(exp, axis=1, keepdims=True)
    if np.any(sums <= 0) or not np.isfinite(sums).all():
        msg = "softmax normalization failed"
        raise ModelError(msg)
    probs = exp / sums
    validate_probability_matrix(probs, outcome_space=outcome_space)
    return np.asarray(probs, dtype=np.float64)


def fit_temperature(
    *,
    logits: np.ndarray,
    labels: tuple[str, ...],
    outcome_space: OutcomeSpace,
) -> TemperatureCalibrationResult:
    """Choose one positive temperature by deterministic bounded search."""
    array = np.asarray(logits, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] != len(labels):
        msg = "calibration logits/labels shape mismatch"
        raise ModelError(msg)
    if not np.isfinite(array).all():
        msg = "calibration logits contain non-finite values"
        raise ModelError(msg)

    temperatures = np.linspace(
        TEMPERATURE_SEARCH_LOW,
        TEMPERATURE_SEARCH_HIGH,
        TEMPERATURE_SEARCH_STEPS,
    )
    best_temperature = 1.0
    best_loss = float("inf")
    for temperature in temperatures:
        probs = softmax(array, outcome_space=outcome_space, temperature=float(temperature))
        loss = multiclass_log_loss(
            labels=labels,
            probabilities=probs,
            outcome_space=outcome_space,
        )
        distance = abs(float(temperature) - 1.0)
        best_distance = abs(best_temperature - 1.0)
        better = (
            loss < best_loss - 1e-15
            or (abs(loss - best_loss) <= 1e-15 and distance < best_distance - 1e-15)
            or (
                abs(loss - best_loss) <= 1e-15
                and abs(distance - best_distance) <= 1e-15
                and float(temperature) < best_temperature
            )
        )
        if better:
            best_loss = loss
            best_temperature = float(temperature)
    return TemperatureCalibrationResult(
        temperature=best_temperature,
        calibration_log_loss=best_loss,
        candidate_count=len(temperatures),
    )

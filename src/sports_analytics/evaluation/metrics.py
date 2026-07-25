"""Typed evaluation metrics for multiclass probability forecasts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.features.contracts import PROBABILITY_SUM_TOLERANCE, OutcomeSpace

ECE_BIN_COUNT: Final[int] = 10
LOG_LOSS_EPS: Final[float] = 1e-15


@dataclass(frozen=True, slots=True)
class ClassDistribution:
    """Observed label counts and frequencies."""

    counts: dict[str, int]
    frequencies: dict[str, float]
    total: int


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One deterministic confidence bin for expected calibration error."""

    bin_index: int
    lower: float
    upper: float
    count: int
    mean_confidence: float | None
    mean_accuracy: float | None
    abs_gap: float | None


@dataclass(frozen=True, slots=True)
class ProbabilityMetrics:
    """Probability-quality metrics for one evaluation region."""

    log_loss: float
    brier_score: float
    accuracy: float
    class_distribution: ClassDistribution
    per_class_recall: dict[str, float]
    expected_calibration_error: float
    calibration_bins: tuple[CalibrationBin, ...]


def encode_labels(*, labels: tuple[str, ...], outcome_space: OutcomeSpace) -> np.ndarray:
    """Encode outcome labels into canonical class indices."""
    index = {label: i for i, label in enumerate(outcome_space.ordered_labels)}
    try:
        return np.asarray([index[label] for label in labels], dtype=np.int64)
    except KeyError as exc:
        msg = f"unsupported outcome label: {exc}"
        raise EvaluationError(msg) from exc


def validate_probability_matrix(
    probabilities: np.ndarray,
    *,
    outcome_space: OutcomeSpace,
    row_count: int | None = None,
) -> np.ndarray:
    """Reject non-finite, out-of-range, or non-normalized probability rows."""
    array = np.asarray(probabilities, dtype=np.float64)
    expected_rows = row_count if row_count is not None else array.shape[0]
    expected_shape = (expected_rows, len(outcome_space.ordered_labels))
    if array.ndim != 2 or array.shape != expected_shape:
        msg = f"probabilities must have shape {expected_shape}, got {array.shape}"
        raise EvaluationError(msg)
    if not np.isfinite(array).all():
        msg = "probabilities contain non-finite values"
        raise EvaluationError(msg)
    if np.any(array < 0.0) or np.any(array > 1.0):
        msg = "probabilities must lie in [0, 1]"
        raise EvaluationError(msg)
    row_sums = np.sum(array, axis=1)
    if np.any(np.abs(row_sums - 1.0) > PROBABILITY_SUM_TOLERANCE):
        msg = "probabilities must sum to one within tolerance"
        raise EvaluationError(msg)
    return array


def multiclass_log_loss(
    *,
    labels: tuple[str, ...],
    probabilities: np.ndarray,
    outcome_space: OutcomeSpace,
) -> float:
    """Mean multiclass log loss (primary probability-quality metric)."""
    probs = validate_probability_matrix(
        probabilities,
        outcome_space=outcome_space,
        row_count=len(labels),
    )
    y = encode_labels(labels=labels, outcome_space=outcome_space)
    clipped = np.clip(probs[np.arange(len(y)), y], LOG_LOSS_EPS, 1.0)
    return float(-np.mean(np.log(clipped)))


def multiclass_brier_score(
    *,
    labels: tuple[str, ...],
    probabilities: np.ndarray,
    outcome_space: OutcomeSpace,
) -> float:
    """Mean multiclass Brier score (primary probability-quality metric)."""
    probs = validate_probability_matrix(
        probabilities,
        outcome_space=outcome_space,
        row_count=len(labels),
    )
    y = encode_labels(labels=labels, outcome_space=outcome_space)
    one_hot = np.zeros_like(probs)
    one_hot[np.arange(len(y)), y] = 1.0
    return float(np.mean(np.sum((probs - one_hot) ** 2, axis=1)))


def accuracy_score(
    *,
    labels: tuple[str, ...],
    probabilities: np.ndarray,
    outcome_space: OutcomeSpace,
) -> float:
    """Argmax accuracy (secondary metric; not primary)."""
    probs = validate_probability_matrix(
        probabilities,
        outcome_space=outcome_space,
        row_count=len(labels),
    )
    pred = np.argmax(probs, axis=1)
    y = encode_labels(labels=labels, outcome_space=outcome_space)
    return float(np.mean(pred == y))


def class_distribution(
    labels: tuple[str, ...],
    *,
    outcome_space: OutcomeSpace,
) -> ClassDistribution:
    """Return ordered class counts and frequencies."""
    ordered = outcome_space.ordered_labels
    counts = {label: 0 for label in ordered}
    for label in labels:
        if label not in counts:
            msg = f"unsupported outcome label: {label}"
            raise EvaluationError(msg)
        counts[label] += 1
    total = len(labels)
    frequencies = {label: (counts[label] / total if total else 0.0) for label in ordered}
    return ClassDistribution(counts=counts, frequencies=frequencies, total=total)


def per_class_recall(
    *,
    labels: tuple[str, ...],
    probabilities: np.ndarray,
    outcome_space: OutcomeSpace,
) -> dict[str, float]:
    """Recall for each outcome using argmax predictions."""
    probs = validate_probability_matrix(
        probabilities,
        outcome_space=outcome_space,
        row_count=len(labels),
    )
    pred = np.argmax(probs, axis=1)
    y = encode_labels(labels=labels, outcome_space=outcome_space)
    recalls: dict[str, float] = {}
    for index, label in enumerate(outcome_space.ordered_labels):
        mask = y == index
        if not np.any(mask):
            recalls[label] = 0.0
        else:
            recalls[label] = float(np.mean(pred[mask] == index))
    return recalls


def expected_calibration_error(
    *,
    labels: tuple[str, ...],
    probabilities: np.ndarray,
    outcome_space: OutcomeSpace,
    bin_count: int = ECE_BIN_COUNT,
) -> tuple[float, tuple[CalibrationBin, ...]]:
    """Deterministic multiclass ECE using max-probability confidence bins."""
    if bin_count < 2:
        msg = "ECE bin_count must be at least 2"
        raise EvaluationError(msg)
    probs = validate_probability_matrix(
        probabilities,
        outcome_space=outcome_space,
        row_count=len(labels),
    )
    y = encode_labels(labels=labels, outcome_space=outcome_space)
    confidence = np.max(probs, axis=1)
    predicted = np.argmax(probs, axis=1)
    correct = (predicted == y).astype(np.float64)
    edges = np.linspace(0.0, 1.0, bin_count + 1)
    bins: list[CalibrationBin] = []
    ece = 0.0
    n = len(labels)
    for bin_index in range(bin_count):
        lower = float(edges[bin_index])
        upper = float(edges[bin_index + 1])
        if bin_index == bin_count - 1:
            mask = (confidence >= lower) & (confidence <= upper)
        else:
            mask = (confidence >= lower) & (confidence < upper)
        count = int(np.sum(mask))
        if count == 0:
            bins.append(
                CalibrationBin(
                    bin_index=bin_index,
                    lower=lower,
                    upper=upper,
                    count=0,
                    mean_confidence=None,
                    mean_accuracy=None,
                    abs_gap=None,
                )
            )
            continue
        mean_confidence = float(np.mean(confidence[mask]))
        mean_accuracy = float(np.mean(correct[mask]))
        gap = abs(mean_confidence - mean_accuracy)
        ece += (count / n) * gap
        bins.append(
            CalibrationBin(
                bin_index=bin_index,
                lower=lower,
                upper=upper,
                count=count,
                mean_confidence=mean_confidence,
                mean_accuracy=mean_accuracy,
                abs_gap=gap,
            )
        )
    return float(ece), tuple(bins)


def evaluate_probabilities(
    *,
    labels: tuple[str, ...],
    probabilities: np.ndarray,
    outcome_space: OutcomeSpace,
) -> ProbabilityMetrics:
    """Compute the full typed metric suite for one region."""
    ece, bins = expected_calibration_error(
        labels=labels,
        probabilities=probabilities,
        outcome_space=outcome_space,
    )
    return ProbabilityMetrics(
        log_loss=multiclass_log_loss(
            labels=labels,
            probabilities=probabilities,
            outcome_space=outcome_space,
        ),
        brier_score=multiclass_brier_score(
            labels=labels,
            probabilities=probabilities,
            outcome_space=outcome_space,
        ),
        accuracy=accuracy_score(
            labels=labels,
            probabilities=probabilities,
            outcome_space=outcome_space,
        ),
        class_distribution=class_distribution(labels, outcome_space=outcome_space),
        per_class_recall=per_class_recall(
            labels=labels,
            probabilities=probabilities,
            outcome_space=outcome_space,
        ),
        expected_calibration_error=ece,
        calibration_bins=bins,
    )


def _parse_decimal_odds_value(value: object, *, outcome_label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        msg = f"invalid decimal odds for {outcome_label}: {value}"
        raise EvaluationError(msg)
    odds = float(value)
    if not np.isfinite(odds) or odds <= 1.0:
        msg = f"invalid decimal odds for {outcome_label}: {odds}"
        raise EvaluationError(msg)
    return odds


def decimal_odds_to_normalized_probabilities(
    *,
    outcome_space: OutcomeSpace,
    decimal_odds: Mapping[str, float] | Sequence[float],
) -> tuple[float, ...]:
    """Convert ordered decimal odds into normalized implied probabilities."""
    labels = outcome_space.ordered_labels
    if isinstance(decimal_odds, Mapping):
        expected_keys = set(labels)
        provided_keys = set(decimal_odds.keys())
        if provided_keys != expected_keys:
            missing = expected_keys - provided_keys
            extra = provided_keys - expected_keys
            if missing:
                missing_label = sorted(missing)[0]
                msg = f"missing decimal odds for outcome: {missing_label}"
                raise EvaluationError(msg)
            extra_label = sorted(extra)[0]
            msg = f"unexpected decimal odds outcome: {extra_label}"
            raise EvaluationError(msg)
        for key in decimal_odds:
            if type(key) is not str:
                msg = "decimal odds mapping keys must be strings"
                raise EvaluationError(msg)
        odds_values = tuple(
            _parse_decimal_odds_value(decimal_odds[label], outcome_label=label) for label in labels
        )
    else:
        if len(decimal_odds) != len(labels):
            msg = (
                "decimal odds count must match outcome space: "
                f"expected {len(labels)}, got {len(decimal_odds)}"
            )
            raise EvaluationError(msg)
        odds_values = tuple(
            _parse_decimal_odds_value(value, outcome_label=label)
            for value, label in zip(decimal_odds, labels, strict=True)
        )
    implied = np.asarray([1.0 / odds for odds in odds_values], dtype=np.float64)
    total = float(np.sum(implied))
    if total <= 0 or not np.isfinite(total):
        msg = "failed to normalize implied probabilities"
        raise EvaluationError(msg)
    normalized = implied / total
    validate_probability_matrix(
        normalized.reshape(1, -1),
        outcome_space=outcome_space,
        row_count=1,
    )
    return tuple(float(value) for value in normalized)


def metrics_to_json(metrics: ProbabilityMetrics) -> dict[str, object]:
    """Serialize metrics into canonical JSON-compatible values."""
    return {
        "log_loss": metrics.log_loss,
        "brier_score": metrics.brier_score,
        "accuracy": metrics.accuracy,
        "class_distribution": {
            "counts": metrics.class_distribution.counts,
            "frequencies": metrics.class_distribution.frequencies,
            "total": metrics.class_distribution.total,
        },
        "per_class_recall": metrics.per_class_recall,
        "expected_calibration_error": metrics.expected_calibration_error,
        "calibration_bins": [
            {
                "bin_index": item.bin_index,
                "lower": item.lower,
                "upper": item.upper,
                "count": item.count,
                "mean_confidence": item.mean_confidence,
                "mean_accuracy": item.mean_accuracy,
                "abs_gap": item.abs_gap,
            }
            for item in metrics.calibration_bins
        ],
        "primary_metrics": ["log_loss", "brier_score"],
        "secondary_metrics": ["accuracy"],
    }

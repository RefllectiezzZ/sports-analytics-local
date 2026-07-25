"""Typed evaluation metrics for multiclass probability forecasts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.models.contracts import OUTCOME_LABELS_1X2

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


def encode_labels(labels: tuple[str, ...]) -> np.ndarray:
    """Encode outcome labels into canonical class indices."""
    index = {label: i for i, label in enumerate(OUTCOME_LABELS_1X2)}
    try:
        return np.asarray([index[label] for label in labels], dtype=np.int64)
    except KeyError as exc:
        msg = f"unsupported outcome label: {exc}"
        raise EvaluationError(msg) from exc


def multiclass_log_loss(*, labels: tuple[str, ...], probabilities: np.ndarray) -> float:
    """Mean multiclass log loss (primary probability-quality metric)."""
    probs = _validate_probs(probabilities, row_count=len(labels))
    y = encode_labels(labels)
    clipped = np.clip(probs[np.arange(len(y)), y], LOG_LOSS_EPS, 1.0)
    return float(-np.mean(np.log(clipped)))


def multiclass_brier_score(*, labels: tuple[str, ...], probabilities: np.ndarray) -> float:
    """Mean multiclass Brier score (primary probability-quality metric)."""
    probs = _validate_probs(probabilities, row_count=len(labels))
    y = encode_labels(labels)
    one_hot = np.zeros_like(probs)
    one_hot[np.arange(len(y)), y] = 1.0
    return float(np.mean(np.sum((probs - one_hot) ** 2, axis=1)))


def accuracy_score(*, labels: tuple[str, ...], probabilities: np.ndarray) -> float:
    """Argmax accuracy (secondary metric; not primary)."""
    probs = _validate_probs(probabilities, row_count=len(labels))
    pred = np.argmax(probs, axis=1)
    y = encode_labels(labels)
    return float(np.mean(pred == y))


def class_distribution(labels: tuple[str, ...]) -> ClassDistribution:
    """Return ordered class counts and frequencies."""
    counts = {label: 0 for label in OUTCOME_LABELS_1X2}
    for label in labels:
        if label not in counts:
            msg = f"unsupported outcome label: {label}"
            raise EvaluationError(msg)
        counts[label] += 1
    total = len(labels)
    frequencies = {label: (counts[label] / total if total else 0.0) for label in OUTCOME_LABELS_1X2}
    return ClassDistribution(counts=counts, frequencies=frequencies, total=total)


def per_class_recall(*, labels: tuple[str, ...], probabilities: np.ndarray) -> dict[str, float]:
    """Recall for each outcome using argmax predictions."""
    probs = _validate_probs(probabilities, row_count=len(labels))
    pred = np.argmax(probs, axis=1)
    y = encode_labels(labels)
    recalls: dict[str, float] = {}
    for index, label in enumerate(OUTCOME_LABELS_1X2):
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
    bin_count: int = ECE_BIN_COUNT,
) -> tuple[float, tuple[CalibrationBin, ...]]:
    """Deterministic multiclass ECE using max-probability confidence bins.

    Bins are equal-width on ``[0, 1]`` with ``bin_count`` bins. The rightmost bin
    is closed on both ends so confidence ``1.0`` is included.
    """
    if bin_count < 2:
        msg = "ECE bin_count must be at least 2"
        raise EvaluationError(msg)
    probs = _validate_probs(probabilities, row_count=len(labels))
    y = encode_labels(labels)
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
) -> ProbabilityMetrics:
    """Compute the full typed metric suite for one region."""
    ece, bins = expected_calibration_error(labels=labels, probabilities=probabilities)
    return ProbabilityMetrics(
        log_loss=multiclass_log_loss(labels=labels, probabilities=probabilities),
        brier_score=multiclass_brier_score(labels=labels, probabilities=probabilities),
        accuracy=accuracy_score(labels=labels, probabilities=probabilities),
        class_distribution=class_distribution(labels),
        per_class_recall=per_class_recall(labels=labels, probabilities=probabilities),
        expected_calibration_error=ece,
        calibration_bins=bins,
    )


def closing_odds_to_normalized_probabilities(
    home_odds: float,
    draw_odds: float,
    away_odds: float,
) -> tuple[float, float, float]:
    """Convert a complete decimal-odds triple to overround-removed probabilities."""
    for name, odds in (
        ("home", home_odds),
        ("draw", draw_odds),
        ("away", away_odds),
    ):
        if not np.isfinite(odds) or odds <= 1.0:
            msg = f"invalid decimal odds for {name}: {odds}"
            raise EvaluationError(msg)
    implied = np.asarray([1.0 / home_odds, 1.0 / draw_odds, 1.0 / away_odds], dtype=np.float64)
    total = float(np.sum(implied))
    if total <= 0 or not np.isfinite(total):
        msg = "failed to normalize implied probabilities"
        raise EvaluationError(msg)
    normalized = implied / total
    return float(normalized[0]), float(normalized[1]), float(normalized[2])


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


def _validate_probs(probabilities: np.ndarray, *, row_count: int) -> np.ndarray:
    array = np.asarray(probabilities, dtype=np.float64)
    if array.ndim != 2 or array.shape != (row_count, len(OUTCOME_LABELS_1X2)):
        msg = (
            "probabilities must have shape "
            f"({row_count}, {len(OUTCOME_LABELS_1X2)}), got {array.shape}"
        )
        raise EvaluationError(msg)
    if not np.isfinite(array).all():
        msg = "probabilities contain non-finite values"
        raise EvaluationError(msg)
    return array

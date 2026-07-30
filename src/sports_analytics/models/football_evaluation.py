"""Production evidence, calibration, uncertainty, and stability gates for football."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import numpy as np

from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.models.football_scores import FootballScoreModel, ScoreTrainingMatch


class EvaluationProvenance(StrEnum):
    SYNTHETIC_CONTRACT = "synthetic-contract"
    SMALL_FIXTURE_CONTRACT = "small-fixture-contract"
    VERIFIED_HISTORICAL = "verified-historical"


@dataclass(frozen=True, slots=True)
class ProductionEvidenceGates:
    """Conservative minimums; fixture-sized data cannot satisfy them."""

    minimum_total_completed_matches: int = 1_000
    minimum_matches_per_competition: int = 300
    minimum_training_rows_per_fold: int = 500
    minimum_calibration_rows_per_fold: int = 100
    minimum_test_rows_per_fold: int = 100
    minimum_rolling_folds: int = 3
    minimum_temporal_span_days: int = 730
    maximum_missing_target_ratio: float = 0.01
    minimum_prediction_coverage: float = 0.95
    maximum_unseen_team_fallback_ratio: float = 0.05
    convergence_required: bool = True

    def __post_init__(self) -> None:
        integer_fields = (
            "minimum_total_completed_matches",
            "minimum_matches_per_competition",
            "minimum_training_rows_per_fold",
            "minimum_calibration_rows_per_fold",
            "minimum_test_rows_per_fold",
            "minimum_rolling_folds",
            "minimum_temporal_span_days",
        )
        if any(
            type(getattr(self, field)) is not int or getattr(self, field) < 1
            for field in integer_fields
        ):
            raise EvaluationError("production evidence integer gates must be positive")
        for field in (
            "maximum_missing_target_ratio",
            "minimum_prediction_coverage",
            "maximum_unseen_team_fallback_ratio",
        ):
            value = getattr(self, field)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise EvaluationError(f"{field} must lie in [0, 1]")


@dataclass(frozen=True, slots=True)
class EvidenceGateResult:
    eligible: bool
    state: str
    reason_codes: tuple[str, ...]


def assess_production_evidence(
    *,
    provenance: EvaluationProvenance,
    completed_matches: int,
    competition_match_counts: tuple[int, ...],
    fold_row_counts: tuple[tuple[int, int, int], ...],
    temporal_start: date | None,
    temporal_end: date | None,
    missing_target_ratio: float,
    prediction_coverage: float,
    unseen_team_fallback_ratio: float,
    all_candidates_converged: bool,
    gates: ProductionEvidenceGates | None = None,
) -> EvidenceGateResult:
    rules = gates or ProductionEvidenceGates()
    reasons: list[str] = []
    if provenance is not EvaluationProvenance.VERIFIED_HISTORICAL:
        reasons.append("evaluation-provenance-not-verified-historical")
    if completed_matches < rules.minimum_total_completed_matches:
        reasons.append("insufficient-total-completed-matches")
    if (
        not competition_match_counts
        or min(competition_match_counts) < rules.minimum_matches_per_competition
    ):
        reasons.append("insufficient-competition-history")
    if len(fold_row_counts) < rules.minimum_rolling_folds:
        reasons.append("insufficient-rolling-folds")
    for training, calibration, test in fold_row_counts:
        if training < rules.minimum_training_rows_per_fold:
            reasons.append("insufficient-training-rows")
        if calibration < rules.minimum_calibration_rows_per_fold:
            reasons.append("insufficient-calibration-rows")
        if test < rules.minimum_test_rows_per_fold:
            reasons.append("insufficient-test-rows")
    if temporal_start is None or temporal_end is None:
        reasons.append("temporal-span-unavailable")
    elif (temporal_end - temporal_start).days < rules.minimum_temporal_span_days:
        reasons.append("insufficient-temporal-span")
    if missing_target_ratio > rules.maximum_missing_target_ratio:
        reasons.append("missing-target-ratio-exceeded")
    if prediction_coverage < rules.minimum_prediction_coverage:
        reasons.append("prediction-coverage-below-minimum")
    if unseen_team_fallback_ratio > rules.maximum_unseen_team_fallback_ratio:
        reasons.append("unseen-team-fallback-ratio-exceeded")
    if rules.convergence_required and not all_candidates_converged:
        reasons.append("candidate-convergence-failed")
    unique = tuple(sorted(set(reasons)))
    return EvidenceGateResult(
        eligible=not unique,
        state="production-eligible" if not unique else "insufficient-real-evaluation-data",
        reason_codes=unique,
    )


@dataclass(frozen=True, slots=True)
class CalibrationDiagnostics:
    intercept: float | None
    slope: float | None
    expected_calibration_error: float | None
    reliability_bins: tuple[tuple[float, float, int], ...]
    sharpness: float | None
    state: str


def multiclass_calibration_diagnostics(
    probabilities: np.ndarray,
    targets: np.ndarray,
    *,
    bin_edges: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    minimum_rows: int = 100,
) -> CalibrationDiagnostics:
    if probabilities.ndim != 2 or probabilities.shape[1] != 3:
        raise EvaluationError("calibration probabilities must have three columns")
    if targets.shape != (probabilities.shape[0],):
        raise EvaluationError("calibration target shape mismatch")
    if probabilities.shape[0] < minimum_rows:
        return CalibrationDiagnostics(None, None, None, (), None, "insufficient-sample")
    confidence = np.max(probabilities, axis=1)
    predicted = np.argmax(probabilities, axis=1)
    correct = (predicted == targets).astype(np.float64)
    bins: list[tuple[float, float, int]] = []
    weighted_gap = 0.0
    for lower, upper in zip(bin_edges[:-1], bin_edges[1:], strict=True):
        mask = (confidence >= lower) & (
            confidence <= upper if upper == bin_edges[-1] else confidence < upper
        )
        count = int(np.sum(mask))
        if not count:
            continue
        mean_confidence = float(np.mean(confidence[mask]))
        accuracy = float(np.mean(correct[mask]))
        bins.append((mean_confidence, accuracy, count))
        weighted_gap += count * abs(mean_confidence - accuracy)
    clipped = np.clip(confidence, 1e-9, 1.0 - 1e-9)
    logits = np.log(clipped / (1.0 - clipped))
    design = np.column_stack((np.ones_like(logits), logits))
    coefficients = np.linalg.lstsq(design, correct, rcond=None)[0]
    return CalibrationDiagnostics(
        intercept=float(coefficients[0]),
        slope=float(coefficients[1]),
        expected_calibration_error=weighted_gap / probabilities.shape[0],
        reliability_bins=tuple(bins),
        sharpness=float(np.std(confidence)),
        state="available",
    )


@dataclass(frozen=True, slots=True)
class BlockBootstrapConfiguration:
    block_size: int = 20
    replicates: int = 500
    confidence_level: float = 0.95
    seed: int = 20260729
    minimum_rows: int = 100

    def __post_init__(self) -> None:
        if type(self.block_size) is not int or self.block_size < 2:
            raise EvaluationError("bootstrap block_size must be at least two")
        if type(self.replicates) is not int or not 100 <= self.replicates <= 5_000:
            raise EvaluationError("bootstrap replicates must lie in [100, 5000]")
        if not 0.5 < self.confidence_level < 1.0:
            raise EvaluationError("bootstrap confidence_level must lie in (0.5, 1)")
        if type(self.seed) is not int:
            raise EvaluationError("bootstrap seed must be an integer")


@dataclass(frozen=True, slots=True)
class MetricInterval:
    estimate: float | None
    lower: float | None
    upper: float | None
    state: str


def paired_temporal_block_interval(
    candidate_losses: tuple[float, ...],
    baseline_losses: tuple[float, ...],
    *,
    configuration: BlockBootstrapConfiguration | None = None,
) -> MetricInterval:
    rules = configuration or BlockBootstrapConfiguration()
    if len(candidate_losses) != len(baseline_losses):
        raise EvaluationError("paired bootstrap inputs must have equal length")
    count = len(candidate_losses)
    if count < max(rules.minimum_rows, rules.block_size * 2):
        return MetricInterval(None, None, None, "insufficient-sample")
    differences = np.asarray(candidate_losses) - np.asarray(baseline_losses)
    if not np.all(np.isfinite(differences)):
        raise EvaluationError("bootstrap losses must be finite")
    starts = np.arange(0, count - rules.block_size + 1)
    rng = np.random.default_rng(rules.seed)
    values = np.empty(rules.replicates, dtype=np.float64)
    blocks_needed = math.ceil(count / rules.block_size)
    for index in range(rules.replicates):
        selected = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate(
            [differences[start : start + rules.block_size] for start in selected]
        )[:count]
        values[index] = float(np.mean(sample))
    alpha = (1.0 - rules.confidence_level) / 2.0
    return MetricInterval(
        estimate=float(np.mean(differences)),
        lower=float(np.quantile(values, alpha)),
        upper=float(np.quantile(values, 1.0 - alpha)),
        state="available",
    )


@dataclass(frozen=True, slots=True)
class RhoStabilityDiagnostics:
    rho: float
    near_boundary: bool
    correction_factors_positive: bool
    low_score_matches: int
    low_score_fraction: float
    state: str
    warning_codes: tuple[str, ...]


def rho_stability_diagnostics(
    model: FootballScoreModel,
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    boundary_fraction: float = 0.1,
) -> RhoStabilityDiagnostics:
    span = model.configuration.rho_maximum - model.configuration.rho_minimum
    distance = min(
        model.rho - model.configuration.rho_minimum,
        model.configuration.rho_maximum - model.rho,
    )
    near = distance <= span * boundary_fraction
    low = sum(item.home_goals <= 1 and item.away_goals <= 1 for item in matches)
    positive = _rho_factors_positive(model)
    warnings: list[str] = []
    if near:
        warnings.append("rho-near-search-boundary")
    if not positive:
        warnings.append("rho-correction-factor-nonpositive")
    if low < 30:
        warnings.append("rho-low-score-sample-insufficient")
    return RhoStabilityDiagnostics(
        rho=model.rho,
        near_boundary=near,
        correction_factors_positive=positive,
        low_score_matches=low,
        low_score_fraction=low / len(matches) if matches else 0.0,
        state="warning" if warnings else "stable",
        warning_codes=tuple(warnings),
    )


def _rho_factors_positive(model: FootballScoreModel) -> bool:
    home, away, _ = model.intensities(
        home_team_id=model.teams[0],
        away_team_id=model.teams[1],
    )
    factors = (
        1.0 - home * away * model.rho,
        1.0 + home * model.rho,
        1.0 + away * model.rho,
        1.0 - model.rho,
    )
    return all(value > 0.0 and math.isfinite(value) for value in factors)

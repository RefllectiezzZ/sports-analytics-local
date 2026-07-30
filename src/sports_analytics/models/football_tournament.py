"""Deterministic rolling-origin tournament for coherent football score models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import numpy as np

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, EvaluationError, ModelError
from sports_analytics.data.types import JsonValue
from sports_analytics.markets.football_score_markets import (
    ScorePredicateKind,
    predicate_probability,
    primitive,
)
from sports_analytics.models.football_evaluation import (
    EvaluationProvenance,
    assess_production_evidence,
)
from sports_analytics.models.football_scores import (
    DIXON_COLES,
    INDEPENDENT_POISSON,
    FootballScoreModel,
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    fit_dixon_coles,
    fit_independent_poisson,
    predict_joint_score,
    temperature_scale_distribution,
)

FOOTBALL_SCORE_TOURNAMENT_TYPE: Final[str] = "football-score-model-tournament"
FOOTBALL_SCORE_TOURNAMENT_SCHEMA: Final[str] = "football-score-model-tournament-v2"


@dataclass(frozen=True, slots=True)
class TournamentSplitConfiguration:
    minimum_training_rows: int = 30
    calibration_rows: int = 10
    test_rows: int = 10
    maximum_folds: int = 5

    def __post_init__(self) -> None:
        for name in (
            "minimum_training_rows",
            "calibration_rows",
            "test_rows",
            "maximum_folds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise EvaluationError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ScoreTournamentCandidate:
    candidate_id: str
    model_family: str
    configuration: ScoreModelConfiguration
    calibration_temperatures: tuple[float, ...] = (0.8, 0.9, 1.0, 1.1, 1.2)

    def __post_init__(self) -> None:
        if self.model_family not in {INDEPENDENT_POISSON, DIXON_COLES}:
            raise EvaluationError("tournament candidate model family is unsupported")
        if not self.candidate_id or not self.calibration_temperatures:
            raise EvaluationError("tournament candidate identity and temperatures are required")
        if any(not math.isfinite(value) or value <= 0.0 for value in self.calibration_temperatures):
            raise EvaluationError("candidate calibration temperatures are invalid")


@dataclass(frozen=True, slots=True)
class TournamentFold:
    fold_id: str
    training: tuple[ScoreTrainingMatch, ...]
    calibration: tuple[ScoreTrainingMatch, ...]
    test: tuple[ScoreTrainingMatch, ...]

    def __post_init__(self) -> None:
        if not self.training or not self.calibration or not self.test:
            raise EvaluationError("tournament fold regions must be non-empty")
        if not max(item.event_date for item in self.training) < min(
            item.event_date for item in self.calibration
        ):
            raise EvaluationError("training must end before calibration starts")
        if not max(item.event_date for item in self.calibration) < min(
            item.event_date for item in self.test
        ):
            raise EvaluationError("calibration must end before test starts")


@dataclass(frozen=True, slots=True)
class TournamentFoldMetric:
    candidate_id: str
    fold_id: str
    training_end: date
    calibration_start: date
    calibration_end: date
    test_start: date
    test_end: date
    temperature: float
    exact_score_negative_log_likelihood: float
    result_log_loss: float
    result_brier: float
    ranked_probability_score: float
    mean_absolute_goal_error: float
    test_rows: int
    converged: bool


@dataclass(frozen=True, slots=True)
class TournamentCandidateSummary:
    candidate_id: str
    model_family: str
    fold_count: int
    exact_score_negative_log_likelihood: float
    result_log_loss: float
    result_brier: float
    ranked_probability_score: float
    mean_absolute_goal_error: float
    all_folds_converged: bool


@dataclass(frozen=True, slots=True)
class FootballScoreTournament:
    candidates: tuple[ScoreTournamentCandidate, ...]
    folds: tuple[TournamentFold, ...]
    fold_metrics: tuple[TournamentFoldMetric, ...]
    summaries: tuple[TournamentCandidateSummary, ...]
    provisional_winner_candidate_id: str | None
    provisional_winner_reason: str
    evaluation_provenance: EvaluationProvenance
    production_eligibility_state: str
    production_ineligibility_reasons: tuple[str, ...]
    multinomial_baseline_state: str
    market_baseline_state: str
    promotion_state: str = "not-promoted-explicit-governance-required"


def default_score_candidates(
    *,
    configuration: ScoreModelConfiguration | None = None,
) -> tuple[ScoreTournamentCandidate, ...]:
    config = configuration or ScoreModelConfiguration()
    return (
        ScoreTournamentCandidate(
            candidate_id="dynamic-independent-poisson-v1",
            model_family=INDEPENDENT_POISSON,
            configuration=config,
        ),
        ScoreTournamentCandidate(
            candidate_id="dynamic-dixon-coles-v1",
            model_family=DIXON_COLES,
            configuration=config,
        ),
    )


def build_tournament_folds(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    configuration: TournamentSplitConfiguration | None = None,
) -> tuple[TournamentFold, ...]:
    """Build expanding-window folds with date boundaries, never row overlap."""
    config = configuration or TournamentSplitConfiguration()
    unique = {item.canonical_event_id: item for item in matches}
    if len(unique) != len(matches):
        raise EvaluationError("tournament matches contain duplicate event identities")
    ordered = tuple(sorted(matches, key=lambda item: (item.event_date, item.canonical_event_id)))
    date_groups: list[tuple[ScoreTrainingMatch, ...]] = []
    for item in ordered:
        if not date_groups or date_groups[-1][0].event_date != item.event_date:
            date_groups.append((item,))
        else:
            date_groups[-1] = (*date_groups[-1], item)
    folds: list[TournamentFold] = []
    training_end_group = 0
    training_rows = 0
    while training_end_group < len(date_groups) and training_rows < config.minimum_training_rows:
        training_rows += len(date_groups[training_end_group])
        training_end_group += 1
    while len(folds) < config.maximum_folds and training_end_group < len(date_groups):
        calibration_end_group = _advance_groups(
            date_groups,
            start=training_end_group,
            minimum_rows=config.calibration_rows,
        )
        test_end_group = _advance_groups(
            date_groups,
            start=calibration_end_group,
            minimum_rows=config.test_rows,
        )
        if calibration_end_group <= training_end_group or test_end_group <= calibration_end_group:
            break
        training = tuple(item for group in date_groups[:training_end_group] for item in group)
        calibration = tuple(
            item
            for group in date_groups[training_end_group:calibration_end_group]
            for item in group
        )
        test = tuple(
            item for group in date_groups[calibration_end_group:test_end_group] for item in group
        )
        folds.append(
            TournamentFold(
                fold_id=f"fold-{len(folds) + 1:02d}",
                training=training,
                calibration=calibration,
                test=test,
            )
        )
        training_end_group = calibration_end_group
    if not folds:
        raise EvaluationError("insufficient chronological regions for a tournament fold")
    return tuple(folds)


def run_score_tournament(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    candidates: tuple[ScoreTournamentCandidate, ...] | None = None,
    split_configuration: TournamentSplitConfiguration | None = None,
    multinomial_baseline_state: str = "retained-existing-baseline-not-score-distribution",
    market_baseline_state: str = "unavailable-no-complete-timestamped-market-input",
    evaluation_provenance: EvaluationProvenance = EvaluationProvenance.SYNTHETIC_CONTRACT,
) -> FootballScoreTournament:
    """Fit/calibrate/test a small predeclared candidate set."""
    registered = candidates or default_score_candidates()
    if len({item.candidate_id for item in registered}) != len(registered):
        raise EvaluationError("tournament candidate ids must be unique")
    folds = build_tournament_folds(matches, configuration=split_configuration)
    metrics: list[TournamentFoldMetric] = []
    for candidate in registered:
        for fold in folds:
            model = _fit_candidate(candidate, fold.training)
            temperature = _select_temperature(
                model,
                fold.calibration,
                candidate.calibration_temperatures,
            )
            metrics.append(
                _evaluate_candidate_fold(
                    candidate=candidate,
                    fold=fold,
                    model=model,
                    temperature=temperature,
                )
            )
    summaries = tuple(_summarize_candidate(candidate, tuple(metrics)) for candidate in registered)
    valid = tuple(item for item in summaries if item.all_folds_converged)
    if valid:
        winner = min(
            valid,
            key=lambda item: (
                item.exact_score_negative_log_likelihood,
                item.result_log_loss,
                item.ranked_probability_score,
                item.candidate_id,
            ),
        )
        winner_id = winner.candidate_id
        reason = (
            "lowest deterministic exact-score NLL among converged candidates; "
            "1X2 log loss and RPS are deterministic tie-breakers; ROI is not used"
        )
    else:
        winner_id = None
        reason = "no candidate passed convergence gates"
    evidence = assess_production_evidence(
        provenance=evaluation_provenance,
        completed_matches=len(matches),
        competition_match_counts=tuple(
            sum(row.competition_id == competition for row in matches)
            for competition in sorted({row.competition_id for row in matches})
        ),
        fold_row_counts=tuple(
            (len(fold.training), len(fold.calibration), len(fold.test)) for fold in folds
        ),
        temporal_start=min(row.event_date for row in matches),
        temporal_end=max(row.event_date for row in matches),
        missing_target_ratio=0.0,
        prediction_coverage=1.0,
        unseen_team_fallback_ratio=0.0,
        all_candidates_converged=all(item.all_folds_converged for item in summaries),
    )
    return FootballScoreTournament(
        candidates=registered,
        folds=folds,
        fold_metrics=tuple(sorted(metrics, key=lambda item: (item.candidate_id, item.fold_id))),
        summaries=tuple(sorted(summaries, key=lambda item: item.candidate_id)),
        provisional_winner_candidate_id=winner_id,
        provisional_winner_reason=reason,
        evaluation_provenance=evaluation_provenance,
        production_eligibility_state=evidence.state,
        production_ineligibility_reasons=evidence.reason_codes,
        multinomial_baseline_state=multinomial_baseline_state,
        market_baseline_state=market_baseline_state,
    )


def tournament_payload(tournament: FootballScoreTournament) -> dict[str, JsonValue]:
    """Serialize immutable candidate configurations, folds, metrics, and decision."""
    return {
        "candidate_configurations": [
            {
                "candidate_id": item.candidate_id,
                "model_family": item.model_family,
                "decay_per_day": item.configuration.decay_per_day,
                "l2_regularization": item.configuration.l2_regularization,
                "maximum_iterations": item.configuration.maximum_iterations,
                "tail_tolerance": item.configuration.tail_tolerance,
                "maximum_grid_goals": item.configuration.maximum_grid_goals,
                "calibration_temperatures": list(item.calibration_temperatures),
            }
            for item in tournament.candidates
        ],
        "folds": [
            {
                "fold_id": item.fold_id,
                "training_start": min(row.event_date for row in item.training).isoformat(),
                "training_end": max(row.event_date for row in item.training).isoformat(),
                "calibration_start": min(row.event_date for row in item.calibration).isoformat(),
                "calibration_end": max(row.event_date for row in item.calibration).isoformat(),
                "test_start": min(row.event_date for row in item.test).isoformat(),
                "test_end": max(row.event_date for row in item.test).isoformat(),
                "training_rows": len(item.training),
                "calibration_rows": len(item.calibration),
                "test_rows": len(item.test),
            }
            for item in tournament.folds
        ],
        "fold_metrics": [_metric_payload(item) for item in tournament.fold_metrics],
        "candidate_summaries": [
            {
                "candidate_id": item.candidate_id,
                "model_family": item.model_family,
                "fold_count": item.fold_count,
                "exact_score_negative_log_likelihood": (item.exact_score_negative_log_likelihood),
                "result_log_loss": item.result_log_loss,
                "result_brier": item.result_brier,
                "ranked_probability_score": item.ranked_probability_score,
                "mean_absolute_goal_error": item.mean_absolute_goal_error,
                "all_folds_converged": item.all_folds_converged,
            }
            for item in tournament.summaries
        ],
        "provisional_winner_candidate_id": tournament.provisional_winner_candidate_id,
        "provisional_winner_reason": tournament.provisional_winner_reason,
        "evaluation_provenance": tournament.evaluation_provenance.value,
        "production_eligibility_state": tournament.production_eligibility_state,
        "production_ineligibility_reasons": list(tournament.production_ineligibility_reasons),
        "multinomial_baseline_state": tournament.multinomial_baseline_state,
        "market_baseline_state": tournament.market_baseline_state,
        "promotion_state": tournament.promotion_state,
        "selection_policy": ("primary-exact-score-nll-then-result-log-loss-rps-no-roi-selection"),
    }


def write_tournament_artifact(
    *,
    root: Path,
    relative_directory: str,
    tournament: FootballScoreTournament,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_SCORE_TOURNAMENT_TYPE,
        schema_version=FOOTBALL_SCORE_TOURNAMENT_SCHEMA,
        payload=tournament_payload(tournament),
    )


def load_tournament_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    """Strictly verify tournament identity plus chronology and candidate linkage."""
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_SCORE_TOURNAMENT_TYPE,
        expected_schema_version=FOOTBALL_SCORE_TOURNAMENT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "candidate_configurations",
        "folds",
        "fold_metrics",
        "candidate_summaries",
        "provisional_winner_candidate_id",
        "provisional_winner_reason",
        "evaluation_provenance",
        "production_eligibility_state",
        "production_ineligibility_reasons",
        "multinomial_baseline_state",
        "market_baseline_state",
        "promotion_state",
        "selection_policy",
    }:
        raise ArtifactError("football tournament payload fields are not exact")
    if payload["promotion_state"] != "not-promoted-explicit-governance-required":
        raise ArtifactError("football tournament claims an implicit promotion")
    if payload["selection_policy"] != (
        "primary-exact-score-nll-then-result-log-loss-rps-no-roi-selection"
    ):
        raise ArtifactError("football tournament selection policy mismatch")
    candidates = payload["candidate_configurations"]
    folds = payload["folds"]
    metrics = payload["fold_metrics"]
    summaries = payload["candidate_summaries"]
    if not isinstance(candidates, list) or not candidates:
        raise ArtifactError("football tournament requires non-empty candidate evidence")
    if not isinstance(folds, list) or not folds:
        raise ArtifactError("football tournament requires non-empty fold evidence")
    if not isinstance(metrics, list) or not metrics:
        raise ArtifactError("football tournament requires non-empty metric evidence")
    if not isinstance(summaries, list) or not summaries:
        raise ArtifactError("football tournament requires non-empty candidate and fold evidence")
    candidate_ids = {
        row.get("candidate_id")
        for row in candidates
        if isinstance(row, dict) and type(row.get("candidate_id")) is str
    }
    fold_ids = {
        row.get("fold_id")
        for row in folds
        if isinstance(row, dict) and type(row.get("fold_id")) is str
    }
    if len(candidate_ids) != len(candidates) or len(fold_ids) != len(folds):
        raise ArtifactError("football tournament identities are duplicate or malformed")
    for row in folds:
        if not isinstance(row, dict):
            raise ArtifactError("football tournament fold is malformed")
        try:
            training_end = date.fromisoformat(str(row["training_end"]))
            calibration_start = date.fromisoformat(str(row["calibration_start"]))
            calibration_end = date.fromisoformat(str(row["calibration_end"]))
            test_start = date.fromisoformat(str(row["test_start"]))
        except (KeyError, ValueError) as exc:
            raise ArtifactError("football tournament fold dates are malformed") from exc
        if not training_end < calibration_start or not calibration_end < test_start:
            raise ArtifactError("football tournament fold chronology is invalid")
    for row in metrics:
        if (
            not isinstance(row, dict)
            or row.get("candidate_id") not in candidate_ids
            or row.get("fold_id") not in fold_ids
        ):
            raise ArtifactError("football tournament metric lineage is invalid")
    winner = payload["provisional_winner_candidate_id"]
    if winner is not None and winner not in candidate_ids:
        raise ArtifactError("football tournament winner is not a registered candidate")
    if payload["evaluation_provenance"] not in {item.value for item in EvaluationProvenance}:
        raise ArtifactError("football tournament evaluation provenance is invalid")
    if payload["production_eligibility_state"] not in {
        "production-eligible",
        "insufficient-real-evaluation-data",
    }:
        raise ArtifactError("football tournament eligibility state is invalid")
    if (
        payload["evaluation_provenance"] != EvaluationProvenance.VERIFIED_HISTORICAL.value
        and payload["production_eligibility_state"] == "production-eligible"
    ):
        raise ArtifactError("fixture tournament cannot claim production eligibility")
    return artifact


def _fit_candidate(
    candidate: ScoreTournamentCandidate,
    training: tuple[ScoreTrainingMatch, ...],
) -> FootballScoreModel:
    try:
        if candidate.model_family == INDEPENDENT_POISSON:
            return fit_independent_poisson(
                training,
                configuration=candidate.configuration,
            )
        return fit_dixon_coles(training, configuration=candidate.configuration)
    except ModelError as exc:
        raise EvaluationError(f"candidate {candidate.candidate_id} failed fitting: {exc}") from exc


def _select_temperature(
    model: FootballScoreModel,
    calibration: tuple[ScoreTrainingMatch, ...],
    temperatures: tuple[float, ...],
) -> float:
    scores: list[tuple[float, float]] = []
    for temperature in temperatures:
        loss = 0.0
        for match in calibration:
            distribution = predict_joint_score(
                model,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
                prediction_cutoff=match.event_date,
            )
            calibrated = temperature_scale_distribution(
                distribution,
                temperature=temperature,
            )
            probability = calibrated.probability(match.home_goals, match.away_goals)
            loss -= math.log(max(probability, 1e-15))
        scores.append((loss / len(calibration), temperature))
    return min(scores, key=lambda item: (item[0], item[1]))[1]


def _evaluate_candidate_fold(
    *,
    candidate: ScoreTournamentCandidate,
    fold: TournamentFold,
    model: FootballScoreModel,
    temperature: float,
) -> TournamentFoldMetric:
    exact_losses: list[float] = []
    result_losses: list[float] = []
    result_briers: list[float] = []
    ranked_scores: list[float] = []
    goal_errors: list[float] = []
    for match in fold.test:
        distribution = temperature_scale_distribution(
            predict_joint_score(
                model,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
                prediction_cutoff=match.event_date,
            ),
            temperature=temperature,
        )
        exact = distribution.probability(match.home_goals, match.away_goals)
        exact_losses.append(-math.log(max(exact, 1e-15)))
        probabilities = (
            predicate_probability(distribution, primitive(ScorePredicateKind.HOME_WIN)),
            predicate_probability(distribution, primitive(ScorePredicateKind.DRAW)),
            predicate_probability(distribution, primitive(ScorePredicateKind.AWAY_WIN)),
        )
        target = (
            0
            if match.home_goals > match.away_goals
            else 1
            if match.home_goals == match.away_goals
            else 2
        )
        result_losses.append(-math.log(max(probabilities[target], 1e-15)))
        result_briers.append(
            math.fsum(
                (probability - (1.0 if index == target else 0.0)) ** 2
                for index, probability in enumerate(probabilities)
            )
            / 3.0
        )
        observed_cumulative = (1.0 if target <= 0 else 0.0, 1.0 if target <= 1 else 0.0)
        predicted_cumulative = (
            probabilities[0],
            probabilities[0] + probabilities[1],
        )
        ranked_scores.append(
            0.5
            * math.fsum(
                (predicted - observed) ** 2
                for predicted, observed in zip(
                    predicted_cumulative,
                    observed_cumulative,
                    strict=True,
                )
            )
        )
        expected_home = math.fsum(
            home * probability
            for home, row in enumerate(distribution.probabilities)
            for probability in row
        )
        expected_away = math.fsum(
            away * probability
            for row in distribution.probabilities
            for away, probability in enumerate(row)
        )
        goal_errors.append(
            (abs(expected_home - match.home_goals) + abs(expected_away - match.away_goals)) / 2.0
        )
    return TournamentFoldMetric(
        candidate_id=candidate.candidate_id,
        fold_id=fold.fold_id,
        training_end=max(item.event_date for item in fold.training),
        calibration_start=min(item.event_date for item in fold.calibration),
        calibration_end=max(item.event_date for item in fold.calibration),
        test_start=min(item.event_date for item in fold.test),
        test_end=max(item.event_date for item in fold.test),
        temperature=temperature,
        exact_score_negative_log_likelihood=float(np.mean(exact_losses)),
        result_log_loss=float(np.mean(result_losses)),
        result_brier=float(np.mean(result_briers)),
        ranked_probability_score=float(np.mean(ranked_scores)),
        mean_absolute_goal_error=float(np.mean(goal_errors)),
        test_rows=len(fold.test),
        converged=model.diagnostics.converged,
    )


def _summarize_candidate(
    candidate: ScoreTournamentCandidate,
    metrics: tuple[TournamentFoldMetric, ...],
) -> TournamentCandidateSummary:
    selected = tuple(item for item in metrics if item.candidate_id == candidate.candidate_id)
    return TournamentCandidateSummary(
        candidate_id=candidate.candidate_id,
        model_family=candidate.model_family,
        fold_count=len(selected),
        exact_score_negative_log_likelihood=float(
            np.mean([item.exact_score_negative_log_likelihood for item in selected])
        ),
        result_log_loss=float(np.mean([item.result_log_loss for item in selected])),
        result_brier=float(np.mean([item.result_brier for item in selected])),
        ranked_probability_score=float(
            np.mean([item.ranked_probability_score for item in selected])
        ),
        mean_absolute_goal_error=float(
            np.mean([item.mean_absolute_goal_error for item in selected])
        ),
        all_folds_converged=all(item.converged for item in selected),
    )


def _advance_groups(
    groups: list[tuple[ScoreTrainingMatch, ...]],
    *,
    start: int,
    minimum_rows: int,
) -> int:
    rows = 0
    index = start
    while index < len(groups) and rows < minimum_rows:
        rows += len(groups[index])
        index += 1
    return index if rows >= minimum_rows else start


def _metric_payload(item: TournamentFoldMetric) -> dict[str, JsonValue]:
    return {
        "candidate_id": item.candidate_id,
        "fold_id": item.fold_id,
        "training_end": item.training_end.isoformat(),
        "calibration_start": item.calibration_start.isoformat(),
        "calibration_end": item.calibration_end.isoformat(),
        "test_start": item.test_start.isoformat(),
        "test_end": item.test_end.isoformat(),
        "temperature": item.temperature,
        "exact_score_negative_log_likelihood": item.exact_score_negative_log_likelihood,
        "result_log_loss": item.result_log_loss,
        "result_brier": item.result_brier,
        "ranked_probability_score": item.ranked_probability_score,
        "mean_absolute_goal_error": item.mean_absolute_goal_error,
        "test_rows": item.test_rows,
        "converged": item.converged,
    }

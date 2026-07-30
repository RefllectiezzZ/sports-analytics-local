"""Unified contract tournament for coherent football score candidates."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import numpy as np

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    build_analytical_artifact_document,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.types import JsonValue
from sports_analytics.features.football.prematch import (
    FinishedTrainingEvent,
    generate_prematch_features,
)
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
)
from sports_analytics.models.calibration import fit_temperature, softmax
from sports_analytics.models.football_challengers import (
    CovariateScoreModel,
    blend_score_surfaces,
    fit_covariate_dixon_coles,
    predict_covariate_score,
    select_ensemble_weights,
)
from sports_analytics.models.football_evaluation import (
    CalibrationDiagnostics,
    EvaluationProvenance,
    MetricInterval,
    assess_production_evidence,
    multiclass_calibration_diagnostics,
    paired_temporal_block_interval,
    rho_stability_diagnostics,
)
from sports_analytics.models.football_scores import (
    FootballScoreModel,
    JointScoreDistribution,
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    fit_dixon_coles,
    fit_independent_poisson,
    predict_joint_score,
)
from sports_analytics.models.football_tournament import (
    TournamentSplitConfiguration,
    build_tournament_folds,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.models.logistic import fit_multinomial_logistic, logits_from_parameters

UNIFIED_TOURNAMENT_TYPE: Final[str] = "football-unified-model-tournament"
UNIFIED_TOURNAMENT_SCHEMA: Final[str] = "football-unified-model-tournament-v2"


@dataclass(frozen=True, slots=True)
class UnifiedFoldMetric:
    candidate_id: str
    fold_id: str
    test_rows: int
    exact_score_nll: float
    result_log_loss: float
    ranked_probability_score: float
    brier_score: float
    mean_absolute_goal_error: float
    poisson_deviance: float
    correct_score_top3_coverage: float
    tail_mass_maximum: float
    prediction_coverage: float


@dataclass(frozen=True, slots=True)
class Compatible1x2Metric:
    candidate_id: str
    fold_id: str
    test_rows: int
    result_log_loss: float
    ranked_probability_score: float
    brier_score: float
    prediction_coverage: float
    limitation: str


@dataclass(frozen=True, slots=True)
class UnifiedTournament:
    metrics: tuple[UnifiedFoldMetric, ...]
    compatible_1x2_metrics: tuple[Compatible1x2Metric, ...]
    provisional_winner_candidate_id: str
    candidate_states: tuple[tuple[str, str], ...]
    calibration_state: str
    uncertainty_state: str
    calibration_diagnostics: CalibrationDiagnostics
    uncertainty_interval: MetricInterval
    test_population_id: str
    fold_windows: tuple[tuple[str, str, str, str, str, str, int, int, int], ...]
    rho_by_fold: tuple[tuple[str, float, str, tuple[str, ...]], ...]
    all_candidates_converged: bool
    rho_warning_codes: tuple[str, ...]
    evaluation_provenance: EvaluationProvenance
    production_eligibility_state: str
    production_ineligibility_reasons: tuple[str, ...]
    promotion_state: str = "not-promoted-explicit-governance-required"


def run_unified_tournament(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    split_configuration: TournamentSplitConfiguration,
    score_configuration: ScoreModelConfiguration | None = None,
    provenance: EvaluationProvenance = EvaluationProvenance.SYNTHETIC_CONTRACT,
    multinomial_baseline_available: bool = False,
    market_baseline_available: bool = False,
    market_probabilities: dict[str, tuple[float, float, float]] | None = None,
) -> UnifiedTournament:
    """Compare every locally applicable score candidate on identical test rows."""
    config = score_configuration or ScoreModelConfiguration()
    folds = build_tournament_folds(matches, configuration=split_configuration)
    metrics: list[UnifiedFoldMetric] = []
    rho_warnings: set[str] = set()
    rho_by_fold: list[tuple[str, float, str, tuple[str, ...]]] = []
    convergence: list[bool] = []
    result_probabilities: dict[str, list[tuple[float, float, float]]] = {}
    result_losses: dict[str, list[float]] = {}
    result_targets: list[int] = []
    test_event_ids: list[str] = []
    compatible_metrics: list[Compatible1x2Metric] = []
    vectors_by_event = _prematch_vectors(matches)
    for fold in folds:
        independent = fit_independent_poisson(fold.training, configuration=config)
        dixon_coles = fit_dixon_coles(fold.training, configuration=config)
        covariate = fit_covariate_dixon_coles(
            fold.training,
            score_configuration=config,
        )
        rho_diagnostics = rho_stability_diagnostics(dixon_coles, fold.training)
        rho_warnings.update(rho_diagnostics.warning_codes)
        rho_by_fold.append(
            (
                fold.fold_id,
                rho_diagnostics.rho,
                rho_diagnostics.state,
                rho_diagnostics.warning_codes,
            )
        )
        convergence.extend(
            (
                independent.diagnostics.converged,
                dixon_coles.diagnostics.converged,
                covariate.base_model.diagnostics.converged,
            )
        )
        calibration_components = _component_surfaces(
            fold.calibration,
            independent=independent,
            dixon_coles=dixon_coles,
            covariate=covariate,
        )
        weights = select_ensemble_weights(
            calibration_components,
            tuple((row.home_goals, row.away_goals) for row in fold.calibration),
        )
        test_components = _component_surfaces(
            fold.test,
            independent=independent,
            dixon_coles=dixon_coles,
            covariate=covariate,
        )
        candidate_surfaces = {
            "dynamic-independent-poisson-v1": test_components[0],
            "dynamic-dixon-coles-v1": test_components[1],
            "covariate-dixon-coles-v1": test_components[2],
            "coherent-score-ensemble-v1": tuple(
                blend_score_surfaces(
                    tuple(component[index] for component in test_components),
                    weights=weights,
                )
                for index in range(len(fold.test))
            ),
        }
        for candidate_id, surfaces in candidate_surfaces.items():
            metrics.append(_evaluate(candidate_id, fold.fold_id, fold.test, surfaces))
            probabilities, targets = _result_arrays(fold.test, surfaces)
            result_probabilities.setdefault(candidate_id, []).extend(
                (float(row[0]), float(row[1]), float(row[2])) for row in probabilities
            )
            result_losses.setdefault(candidate_id, []).extend(
                -math.log(max(float(row[int(target)]), 1e-15))
                for row, target in zip(probabilities, targets, strict=True)
            )
        result_targets.extend(
            0 if row.home_goals > row.away_goals else 1 if row.home_goals == row.away_goals else 2
            for row in fold.test
        )
        test_event_ids.extend(row.canonical_event_id for row in fold.test)
        if multinomial_baseline_available:
            compatible_metrics.append(
                _multinomial_baseline_metric(
                    fold.fold_id,
                    fold.training,
                    fold.calibration,
                    fold.test,
                    vectors_by_event,
                )
            )
        if market_baseline_available and market_probabilities is not None:
            compatible_metrics.append(
                _market_baseline_metric(
                    fold.fold_id,
                    fold.test,
                    market_probabilities,
                )
            )
    summaries = {
        candidate: float(
            np.mean([row.exact_score_nll for row in metrics if row.candidate_id == candidate])
        )
        for candidate in sorted({row.candidate_id for row in metrics})
    }
    winner = min(summaries, key=lambda candidate: (summaries[candidate], candidate))
    test_probabilities = np.asarray(result_probabilities[winner], dtype=np.float64)
    test_targets = np.asarray(result_targets, dtype=np.int64)
    calibration = multiclass_calibration_diagnostics(
        test_probabilities,
        test_targets,
    )
    uncertainty = paired_temporal_block_interval(
        tuple(result_losses[winner]),
        tuple(result_losses["dynamic-independent-poisson-v1"]),
    )
    evidence = assess_production_evidence(
        provenance=provenance,
        completed_matches=len(matches),
        competition_match_counts=(len(matches),),
        fold_row_counts=tuple(
            (len(fold.training), len(fold.calibration), len(fold.test)) for fold in folds
        ),
        temporal_start=min(row.event_date for row in matches),
        temporal_end=max(row.event_date for row in matches),
        missing_target_ratio=0.0,
        prediction_coverage=min(row.prediction_coverage for row in metrics),
        unseen_team_fallback_ratio=0.0,
        all_candidates_converged=all(convergence),
    )
    population_id = content_addressed_id(
        identity_type="football-unified-test-population-v1",
        payload={"canonical_event_ids": cast(list[JsonValue], sorted(test_event_ids))},
    )
    return UnifiedTournament(
        metrics=tuple(sorted(metrics, key=lambda row: (row.candidate_id, row.fold_id))),
        compatible_1x2_metrics=tuple(
            sorted(compatible_metrics, key=lambda row: (row.candidate_id, row.fold_id))
        ),
        provisional_winner_candidate_id=winner,
        candidate_states=(
            (
                "multinomial-logistic-1x2-v1",
                (
                    "available-compatible-1x2-only-separate-benchmark"
                    if multinomial_baseline_available
                    else "unavailable-no-common-feature-artifact"
                ),
            ),
            ("dynamic-independent-poisson-v1", "evaluated"),
            ("dynamic-dixon-coles-v1", "evaluated"),
            ("covariate-dixon-coles-v1", "evaluated"),
            ("coherent-score-ensemble-v1", "evaluated"),
            (
                "historical-market-only-1x2",
                (
                    "available-compatible-1x2-only-separate-benchmark"
                    if market_baseline_available
                    else "unavailable-no-aligned-closing-quotes"
                ),
            ),
            ("market-aware-score", "excluded-no-timestamp-compatible-input"),
        ),
        calibration_state=calibration.state,
        uncertainty_state=uncertainty.state,
        calibration_diagnostics=calibration,
        uncertainty_interval=uncertainty,
        test_population_id=population_id,
        fold_windows=tuple(
            (
                fold.fold_id,
                min(row.event_date for row in fold.training).isoformat(),
                max(row.event_date for row in fold.training).isoformat(),
                min(row.event_date for row in fold.calibration).isoformat(),
                max(row.event_date for row in fold.calibration).isoformat(),
                max(row.event_date for row in fold.test).isoformat(),
                len(fold.training),
                len(fold.calibration),
                len(fold.test),
            )
            for fold in folds
        ),
        rho_by_fold=tuple(rho_by_fold),
        all_candidates_converged=all(convergence),
        rho_warning_codes=tuple(sorted(rho_warnings)),
        evaluation_provenance=provenance,
        production_eligibility_state=evidence.state,
        production_ineligibility_reasons=evidence.reason_codes,
    )


def write_unified_tournament_artifact(
    *,
    root: Path,
    relative_directory: str,
    tournament: UnifiedTournament,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=UNIFIED_TOURNAMENT_TYPE,
        schema_version=UNIFIED_TOURNAMENT_SCHEMA,
        payload=_payload(tournament),
    )


def unified_tournament_artifact_id(tournament: UnifiedTournament) -> str:
    """Return the exact immutable identity expected from a tournament publication."""
    document = build_analytical_artifact_document(
        artifact_type=UNIFIED_TOURNAMENT_TYPE,
        schema_version=UNIFIED_TOURNAMENT_SCHEMA,
        payload=_payload(tournament),
    )
    artifact_id = document["artifact_id"]
    if not isinstance(artifact_id, str):
        raise ArtifactError("unified tournament artifact identity is invalid")
    return artifact_id


def load_unified_tournament_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=UNIFIED_TOURNAMENT_TYPE,
        expected_schema_version=UNIFIED_TOURNAMENT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    expected = {
        "metrics",
        "compatible_1x2_metrics",
        "provisional_winner_candidate_id",
        "candidate_states",
        "calibration_state",
        "uncertainty_state",
        "calibration_diagnostics",
        "uncertainty_interval",
        "test_population_id",
        "fold_windows",
        "rho_by_fold",
        "all_candidates_converged",
        "rho_warning_codes",
        "evaluation_provenance",
        "production_eligibility_state",
        "production_ineligibility_reasons",
        "promotion_state",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ArtifactError("unified tournament payload fields are not exact")
    if payload["promotion_state"] != "not-promoted-explicit-governance-required":
        raise ArtifactError("unified tournament claims implicit promotion")
    if (
        payload["evaluation_provenance"] != EvaluationProvenance.VERIFIED_HISTORICAL.value
        and payload["production_eligibility_state"] == "production-eligible"
    ):
        raise ArtifactError("contract tournament cannot be production eligible")
    return artifact


def _component_surfaces(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    independent: FootballScoreModel,
    dixon_coles: FootballScoreModel,
    covariate: CovariateScoreModel,
) -> tuple[tuple[JointScoreDistribution, ...], ...]:
    return (
        tuple(
            predict_joint_score(
                independent,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                prediction_cutoff=row.event_date,
            )
            for row in matches
        ),
        tuple(
            predict_joint_score(
                dixon_coles,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                prediction_cutoff=row.event_date,
            )
            for row in matches
        ),
        tuple(
            predict_covariate_score(
                covariate,
                home_team_id=row.home_team_id,
                away_team_id=row.away_team_id,
                prediction_cutoff=row.event_date,
            )
            for row in matches
        ),
    )


def _evaluate(
    candidate_id: str,
    fold_id: str,
    matches: tuple[ScoreTrainingMatch, ...],
    surfaces: tuple[JointScoreDistribution, ...],
) -> UnifiedFoldMetric:
    exact_losses: list[float] = []
    result_losses: list[float] = []
    rps: list[float] = []
    brier: list[float] = []
    goal_errors: list[float] = []
    deviances: list[float] = []
    top3 = 0
    for match, surface in zip(matches, surfaces, strict=True):
        exact = surface.probability(match.home_goals, match.away_goals)
        exact_losses.append(-math.log(max(exact, 1e-15)))
        probabilities = _result_probabilities(surface)
        target = (
            0
            if match.home_goals > match.away_goals
            else 1
            if match.home_goals == match.away_goals
            else 2
        )
        result_losses.append(-math.log(max(probabilities[target], 1e-15)))
        brier.append(sum((p - (index == target)) ** 2 for index, p in enumerate(probabilities)) / 3)
        rps.append(
            (
                (probabilities[0] - (target == 0)) ** 2
                + (probabilities[0] + probabilities[1] - (target <= 1)) ** 2
            )
            / 2
        )
        goal_errors.append(
            (
                abs(surface.home_intensity - match.home_goals)
                + abs(surface.away_intensity - match.away_goals)
            )
            / 2
        )
        deviances.append(
            _poisson_deviance(match.home_goals, surface.home_intensity)
            + _poisson_deviance(match.away_goals, surface.away_intensity)
        )
        ranked = sorted(
            (
                (probability, home, away)
                for home, row in enumerate(surface.probabilities)
                for away, probability in enumerate(row)
            ),
            reverse=True,
        )[:3]
        top3 += any(
            home == match.home_goals and away == match.away_goals for _, home, away in ranked
        )
    return UnifiedFoldMetric(
        candidate_id,
        fold_id,
        len(matches),
        float(np.mean(exact_losses)),
        float(np.mean(result_losses)),
        float(np.mean(rps)),
        float(np.mean(brier)),
        float(np.mean(goal_errors)),
        float(np.mean(deviances)),
        top3 / len(matches),
        max(surface.residual_tail_mass for surface in surfaces),
        len(surfaces) / len(matches),
    )


def _result_probabilities(surface: JointScoreDistribution) -> tuple[float, float, float]:
    home = draw = away = 0.0
    for home_goals, row in enumerate(surface.probabilities):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away += probability
    return home, draw, away


def _result_arrays(
    matches: tuple[ScoreTrainingMatch, ...],
    surfaces: tuple[JointScoreDistribution, ...],
) -> tuple[np.ndarray, np.ndarray]:
    probabilities = np.asarray([_result_probabilities(surface) for surface in surfaces])
    targets = np.asarray(
        [
            0 if row.home_goals > row.away_goals else 1 if row.home_goals == row.away_goals else 2
            for row in matches
        ]
    )
    return probabilities, targets


def _poisson_deviance(observed: int, expected: float) -> float:
    return 2.0 * (
        expected
        if observed == 0
        else observed * math.log(observed / expected) - observed + expected
    )


def _payload(tournament: UnifiedTournament) -> dict[str, JsonValue]:
    return {
        "metrics": [
            {
                "candidate_id": row.candidate_id,
                "fold_id": row.fold_id,
                "test_rows": row.test_rows,
                "exact_score_nll": row.exact_score_nll,
                "result_log_loss": row.result_log_loss,
                "ranked_probability_score": row.ranked_probability_score,
                "brier_score": row.brier_score,
                "mean_absolute_goal_error": row.mean_absolute_goal_error,
                "poisson_deviance": row.poisson_deviance,
                "correct_score_top3_coverage": row.correct_score_top3_coverage,
                "tail_mass_maximum": row.tail_mass_maximum,
                "prediction_coverage": row.prediction_coverage,
            }
            for row in tournament.metrics
        ],
        "compatible_1x2_metrics": [
            {
                "candidate_id": row.candidate_id,
                "fold_id": row.fold_id,
                "test_rows": row.test_rows,
                "result_log_loss": row.result_log_loss,
                "ranked_probability_score": row.ranked_probability_score,
                "brier_score": row.brier_score,
                "prediction_coverage": row.prediction_coverage,
                "limitation": row.limitation,
            }
            for row in tournament.compatible_1x2_metrics
        ],
        "provisional_winner_candidate_id": tournament.provisional_winner_candidate_id,
        "candidate_states": [
            {"candidate_id": candidate, "state": state}
            for candidate, state in tournament.candidate_states
        ],
        "calibration_state": tournament.calibration_state,
        "uncertainty_state": tournament.uncertainty_state,
        "calibration_diagnostics": {
            "intercept": tournament.calibration_diagnostics.intercept,
            "slope": tournament.calibration_diagnostics.slope,
            "expected_calibration_error": (
                tournament.calibration_diagnostics.expected_calibration_error
            ),
            "reliability_bins": [
                {
                    "mean_confidence": confidence,
                    "accuracy": accuracy,
                    "rows": rows,
                }
                for confidence, accuracy, rows in (
                    tournament.calibration_diagnostics.reliability_bins
                )
            ],
            "sharpness": tournament.calibration_diagnostics.sharpness,
            "state": tournament.calibration_diagnostics.state,
        },
        "uncertainty_interval": {
            "estimate": tournament.uncertainty_interval.estimate,
            "lower": tournament.uncertainty_interval.lower,
            "upper": tournament.uncertainty_interval.upper,
            "state": tournament.uncertainty_interval.state,
        },
        "test_population_id": tournament.test_population_id,
        "fold_windows": [
            {
                "fold_id": fold_id,
                "training_start": training_start,
                "training_end": training_end,
                "calibration_start": calibration_start,
                "calibration_end": calibration_end,
                "test_end": test_end,
                "training_rows": training_rows,
                "calibration_rows": calibration_rows,
                "test_rows": test_rows,
            }
            for (
                fold_id,
                training_start,
                training_end,
                calibration_start,
                calibration_end,
                test_end,
                training_rows,
                calibration_rows,
                test_rows,
            ) in tournament.fold_windows
        ],
        "rho_by_fold": [
            {
                "fold_id": fold_id,
                "rho": rho,
                "state": state,
                "warning_codes": list(warnings),
            }
            for fold_id, rho, state, warnings in tournament.rho_by_fold
        ],
        "all_candidates_converged": tournament.all_candidates_converged,
        "rho_warning_codes": list(tournament.rho_warning_codes),
        "evaluation_provenance": tournament.evaluation_provenance.value,
        "production_eligibility_state": tournament.production_eligibility_state,
        "production_ineligibility_reasons": list(tournament.production_ineligibility_reasons),
        "promotion_state": tournament.promotion_state,
    }


def _prematch_vectors(
    matches: tuple[ScoreTrainingMatch, ...],
) -> dict[str, tuple[tuple[float, ...], str]]:
    events = tuple(
        FinishedTrainingEvent(
            canonical_event_id=row.canonical_event_id,
            sport_code="football",
            competition_id=row.competition_id,
            season_id=f"{row.competition_id}:historical",
            event_date=row.event_date,
            scheduled_start_utc=None,
            home_canonical_participant_id=row.home_team_id,
            away_canonical_participant_id=row.away_team_id,
            home_score=row.home_goals,
            away_score=row.away_goals,
            result_code=(
                "home"
                if row.home_goals > row.away_goals
                else "away"
                if row.away_goals > row.home_goals
                else "draw"
            ),
        )
        for row in matches
    )
    return {
        item.metadata.canonical_event_id: (item.ordered_values(), item.result_code)
        for item in generate_prematch_features(events)
    }


def _multinomial_baseline_metric(
    fold_id: str,
    training: tuple[ScoreTrainingMatch, ...],
    calibration: tuple[ScoreTrainingMatch, ...],
    test: tuple[ScoreTrainingMatch, ...],
    vectors: dict[str, tuple[tuple[float, ...], str]],
) -> Compatible1x2Metric:
    training_rows = [vectors[row.canonical_event_id] for row in training]
    calibration_rows = [vectors[row.canonical_event_id] for row in calibration]
    test_rows = [vectors[row.canonical_event_id] for row in test]
    parameters = fit_multinomial_logistic(
        feature_matrix=np.asarray([row[0] for row in training_rows], dtype=np.float64),
        labels=tuple(row[1] for row in training_rows),
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    )
    calibration_logits = logits_from_parameters(
        feature_vector=np.asarray([row[0] for row in calibration_rows], dtype=np.float64),
        parameters=parameters,
    )
    temperature = fit_temperature(
        logits=calibration_logits,
        labels=tuple(row[1] for row in calibration_rows),
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
    ).temperature
    test_logits = logits_from_parameters(
        feature_vector=np.asarray([row[0] for row in test_rows], dtype=np.float64),
        parameters=parameters,
    )
    probabilities = softmax(
        test_logits,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        temperature=temperature,
    )
    targets = np.asarray(
        [FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels.index(row[1]) for row in test_rows],
        dtype=np.int64,
    )
    return _compatible_metric(
        "multinomial-logistic-1x2-v1",
        fold_id,
        probabilities,
        targets,
        "1X2-only candidate; no joint score matrix",
    )


def _market_baseline_metric(
    fold_id: str,
    test: tuple[ScoreTrainingMatch, ...],
    market_probabilities: dict[str, tuple[float, float, float]],
) -> Compatible1x2Metric:
    aligned = [
        (row, market_probabilities[row.canonical_event_id])
        for row in test
        if row.canonical_event_id in market_probabilities
    ]
    probabilities = np.asarray([item[1] for item in aligned], dtype=np.float64)
    targets = np.asarray(
        [
            0
            if item[0].home_goals > item[0].away_goals
            else 1
            if item[0].home_goals == item[0].away_goals
            else 2
            for item in aligned
        ],
        dtype=np.int64,
    )
    return _compatible_metric(
        "historical-market-only-1x2",
        fold_id,
        probabilities,
        targets,
        "historical-closing-benchmark; not executable historical strategy",
        denominator=len(test),
    )


def _compatible_metric(
    candidate_id: str,
    fold_id: str,
    probabilities: np.ndarray,
    targets: np.ndarray,
    limitation: str,
    *,
    denominator: int | None = None,
) -> Compatible1x2Metric:
    if probabilities.shape[0] == 0:
        return Compatible1x2Metric(
            candidate_id,
            fold_id,
            0,
            float("inf"),
            float("inf"),
            float("inf"),
            0.0,
            limitation,
        )
    losses = -np.log(np.clip(probabilities[np.arange(len(targets)), targets], 1e-15, 1.0))
    brier = [
        sum(
            (float(probability) - (index == int(target))) ** 2
            for index, probability in enumerate(row)
        )
        / 3
        for row, target in zip(probabilities, targets, strict=True)
    ]
    rps = [
        (
            (float(row[0]) - (int(target) == 0)) ** 2
            + (float(row[0] + row[1]) - (int(target) <= 1)) ** 2
        )
        / 2
        for row, target in zip(probabilities, targets, strict=True)
    ]
    return Compatible1x2Metric(
        candidate_id=candidate_id,
        fold_id=fold_id,
        test_rows=len(targets),
        result_log_loss=float(np.mean(losses)),
        ranked_probability_score=float(np.mean(rps)),
        brier_score=float(np.mean(brier)),
        prediction_coverage=len(targets) / (denominator or len(targets)),
        limitation=limitation,
    )

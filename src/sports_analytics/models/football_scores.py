"""Deterministic dynamic football score models and coherent score surfaces.

The fitted representation is deliberately small and JSON-safe.  Team attack and
defence effects are estimated with time-decayed Poisson likelihood, explicit
sum-to-zero constraints, and L2 regularization.  Dixon-Coles extends the fitted
Poisson intensities with the reviewed four-cell low-score correction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Final

import numpy as np

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.types import JsonValue

FOOTBALL_SCORE_MODEL_VERSION: Final[str] = "football-dynamic-score-v1"
FOOTBALL_SCORE_ARTIFACT_TYPE: Final[str] = "football-score-model"
FOOTBALL_SCORE_ARTIFACT_SCHEMA: Final[str] = "football-score-model-v1"
INDEPENDENT_POISSON: Final[str] = "independent-poisson"
DIXON_COLES: Final[str] = "dixon-coles"
_MODEL_FAMILIES: Final[frozenset[str]] = frozenset({INDEPENDENT_POISSON, DIXON_COLES})


@dataclass(frozen=True, slots=True)
class ScoreTrainingMatch:
    """One finished match available to a score-model fit."""

    canonical_event_id: str
    competition_id: str
    event_date: date
    home_team_id: str
    away_team_id: str
    home_goals: int
    away_goals: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.canonical_event_id,
                self.competition_id,
                self.home_team_id,
                self.away_team_id,
            )
        ):
            raise ModelError("score training identities must be non-empty")
        if self.home_team_id == self.away_team_id:
            raise ModelError("a team cannot play itself")
        if (
            type(self.home_goals) is not int
            or type(self.away_goals) is not int
            or self.home_goals < 0
            or self.away_goals < 0
        ):
            raise ModelError("score targets must be non-negative integers")


@dataclass(frozen=True, slots=True)
class ScoreModelConfiguration:
    """Bounded deterministic optimizer and probability-grid configuration."""

    decay_per_day: float = 0.0015
    l2_regularization: float = 0.02
    learning_rate: float = 0.08
    maximum_iterations: int = 2_000
    convergence_tolerance: float = 1e-8
    minimum_matches: int = 12
    minimum_grid_goals: int = 7
    maximum_grid_goals: int = 16
    tail_tolerance: float = 1e-7
    rho_minimum: float = -0.2
    rho_maximum: float = 0.2
    rho_grid_points: int = 161

    def __post_init__(self) -> None:
        finite_non_negative = (
            ("decay_per_day", self.decay_per_day),
            ("l2_regularization", self.l2_regularization),
        )
        for name, value in finite_non_negative:
            if not math.isfinite(value) or value < 0.0:
                raise ModelError(f"{name} must be finite and non-negative")
        if not math.isfinite(self.learning_rate) or self.learning_rate <= 0.0:
            raise ModelError("learning_rate must be finite and positive")
        if not math.isfinite(self.convergence_tolerance) or self.convergence_tolerance <= 0.0:
            raise ModelError("convergence_tolerance must be finite and positive")
        for name in (
            "maximum_iterations",
            "minimum_matches",
            "minimum_grid_goals",
            "maximum_grid_goals",
            "rho_grid_points",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ModelError(f"{name} must be a positive integer")
        if self.minimum_grid_goals > self.maximum_grid_goals:
            raise ModelError("minimum_grid_goals cannot exceed maximum_grid_goals")
        if not 0.0 < self.tail_tolerance < 0.01:
            raise ModelError("tail_tolerance must lie in (0, 0.01)")
        if not -0.5 < self.rho_minimum <= 0.0 <= self.rho_maximum < 0.5:
            raise ModelError("rho bounds must be stable and contain zero")
        if self.rho_grid_points < 3:
            raise ModelError("rho_grid_points must be at least three")


@dataclass(frozen=True, slots=True)
class ScoreModelDiagnostics:
    """Exact bounded optimizer result."""

    converged: bool
    iterations: int
    objective: float
    gradient_norm: float
    rho_candidates_evaluated: int
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FootballScoreModel:
    """Safe fitted dynamic Poisson or Dixon-Coles model."""

    model_family: str
    competition_id: str
    training_start: date
    training_end: date
    teams: tuple[str, ...]
    base_log_rate: float
    home_advantage: float
    attack_strengths: tuple[float, ...]
    defence_strengths: tuple[float, ...]
    rho: float
    configuration: ScoreModelConfiguration
    diagnostics: ScoreModelDiagnostics

    def __post_init__(self) -> None:
        if self.model_family not in _MODEL_FAMILIES:
            raise ModelError("unsupported football score model family")
        if not self.competition_id or not self.teams:
            raise ModelError("model competition and teams must be non-empty")
        if self.training_start > self.training_end:
            raise ModelError("training interval is reversed")
        if tuple(sorted(self.teams)) != self.teams or len(set(self.teams)) != len(self.teams):
            raise ModelError("model teams must be unique and canonically ordered")
        if len(self.attack_strengths) != len(self.teams) or len(self.defence_strengths) != len(
            self.teams
        ):
            raise ModelError("team parameter lengths do not match team identities")
        values = (
            self.base_log_rate,
            self.home_advantage,
            self.rho,
            self.diagnostics.objective,
            self.diagnostics.gradient_norm,
            *self.attack_strengths,
            *self.defence_strengths,
        )
        if any(not math.isfinite(value) for value in values):
            raise ModelError("model parameters and diagnostics must be finite")
        if self.model_family == INDEPENDENT_POISSON and self.rho != 0.0:
            raise ModelError("independent Poisson requires rho=0")
        if not self.configuration.rho_minimum <= self.rho <= self.configuration.rho_maximum:
            raise ModelError("rho is outside the configured stable bounds")
        if abs(math.fsum(self.attack_strengths)) > 1e-8:
            raise ModelError("attack effects violate the sum-to-zero constraint")
        if abs(math.fsum(self.defence_strengths)) > 1e-8:
            raise ModelError("defence effects violate the sum-to-zero constraint")

    def intensities(self, *, home_team_id: str, away_team_id: str) -> tuple[float, float, bool]:
        """Return intensities and whether competition-average fallback was used."""
        if home_team_id == away_team_id:
            raise ModelError("a team cannot play itself")
        indexes = {team: index for index, team in enumerate(self.teams)}
        home_index = indexes.get(home_team_id)
        away_index = indexes.get(away_team_id)
        fallback = home_index is None or away_index is None
        home_attack = 0.0 if home_index is None else self.attack_strengths[home_index]
        home_defence = 0.0 if home_index is None else self.defence_strengths[home_index]
        away_attack = 0.0 if away_index is None else self.attack_strengths[away_index]
        away_defence = 0.0 if away_index is None else self.defence_strengths[away_index]
        home = math.exp(self.base_log_rate + self.home_advantage + home_attack - away_defence)
        away = math.exp(self.base_log_rate + away_attack - home_defence)
        if not all(math.isfinite(value) and value > 0.0 for value in (home, away)):
            raise ModelError("fitted model produced invalid goal intensities")
        return home, away, fallback


@dataclass(frozen=True, slots=True)
class JointScoreDistribution:
    """Immutable normalized full-time joint score probability surface."""

    probabilities: tuple[tuple[float, ...], ...]
    home_intensity: float
    away_intensity: float
    rho: float
    score_grid_maximum: int
    residual_tail_mass: float
    tail_tolerance: float
    model_family: str
    model_version: str
    competition_id: str
    prediction_cutoff: date
    fallback_used: bool
    calibration_method: str = "none"

    def __post_init__(self) -> None:
        expected = self.score_grid_maximum + 1
        if expected < 2 or len(self.probabilities) != expected:
            raise ModelError("joint score grid shape is invalid")
        if any(len(row) != expected for row in self.probabilities):
            raise ModelError("joint score grid must be square")
        values = tuple(value for row in self.probabilities for value in row)
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ModelError("joint score probabilities must be finite and non-negative")
        if not math.isclose(math.fsum(values), 1.0, rel_tol=0.0, abs_tol=1e-11):
            raise ModelError("joint score probabilities must sum to one")
        if (
            not math.isfinite(self.residual_tail_mass)
            or self.residual_tail_mass < 0.0
            or self.residual_tail_mass > self.tail_tolerance
        ):
            raise ModelError("residual tail mass violates the configured tolerance")

    def probability(self, home_goals: int, away_goals: int) -> float:
        if not 0 <= home_goals <= self.score_grid_maximum:
            return 0.0
        if not 0 <= away_goals <= self.score_grid_maximum:
            return 0.0
        return self.probabilities[home_goals][away_goals]


def fit_independent_poisson(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    configuration: ScoreModelConfiguration | None = None,
) -> FootballScoreModel:
    """Fit a deterministic time-decayed independent Poisson score model."""
    config = configuration or ScoreModelConfiguration()
    return _fit_base_model(matches, configuration=config, model_family=INDEPENDENT_POISSON)


def fit_dixon_coles(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    configuration: ScoreModelConfiguration | None = None,
) -> FootballScoreModel:
    """Fit Poisson strengths and a deterministic Dixon-Coles rho grid."""
    config = configuration or ScoreModelConfiguration()
    base = _fit_base_model(matches, configuration=config, model_family=INDEPENDENT_POISSON)
    ordered = _validate_matches(matches, config)
    weights = _time_weights(ordered, decay_per_day=config.decay_per_day)
    candidates = np.linspace(config.rho_minimum, config.rho_maximum, config.rho_grid_points)
    best_rho: float | None = None
    best_objective = math.inf
    for raw_rho in candidates:
        rho = float(raw_rho)
        objective = _rho_objective(base, ordered, weights, rho)
        if objective < best_objective - 1e-14:
            best_objective = objective
            best_rho = rho
    if best_rho is None or not math.isfinite(best_objective):
        raise ModelError("Dixon-Coles rho search found no valid parameter state")
    return replace(
        base,
        model_family=DIXON_COLES,
        rho=best_rho,
        diagnostics=replace(
            base.diagnostics,
            objective=base.diagnostics.objective + best_objective,
            rho_candidates_evaluated=config.rho_grid_points,
        ),
    )


def predict_joint_score(
    model: FootballScoreModel,
    *,
    home_team_id: str,
    away_team_id: str,
    prediction_cutoff: date,
) -> JointScoreDistribution:
    """Build one normalized coherent score matrix with explicit tail handling."""
    if prediction_cutoff <= model.training_end:
        raise ModelError("prediction cutoff must be after the model training interval")
    home_intensity, away_intensity, fallback = model.intensities(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    return joint_score_from_intensities(
        home_intensity=home_intensity,
        away_intensity=away_intensity,
        rho=model.rho,
        model_family=model.model_family,
        competition_id=model.competition_id,
        prediction_cutoff=prediction_cutoff,
        configuration=model.configuration,
        fallback_used=fallback,
    )


def joint_score_from_intensities(
    *,
    home_intensity: float,
    away_intensity: float,
    rho: float = 0.0,
    model_family: str = INDEPENDENT_POISSON,
    competition_id: str = "synthetic",
    prediction_cutoff: date,
    configuration: ScoreModelConfiguration | None = None,
    fallback_used: bool = False,
) -> JointScoreDistribution:
    """Construct a reviewed score surface directly from two intensities."""
    config = configuration or ScoreModelConfiguration()
    if model_family not in _MODEL_FAMILIES:
        raise ModelError("unsupported football score model family")
    if model_family == INDEPENDENT_POISSON and rho != 0.0:
        raise ModelError("independent Poisson score surfaces require rho=0")
    if not all(math.isfinite(value) and value > 0.0 for value in (home_intensity, away_intensity)):
        raise ModelError("goal intensities must be finite and positive")
    if not config.rho_minimum <= rho <= config.rho_maximum or not math.isfinite(rho):
        raise ModelError("rho is outside configured stable bounds")
    maximum = _select_grid_maximum(home_intensity, away_intensity, config)
    home_probabilities = _poisson_probabilities(home_intensity, maximum)
    away_probabilities = _poisson_probabilities(away_intensity, maximum)
    base_mass = math.fsum(home_probabilities) * math.fsum(away_probabilities)
    residual = max(0.0, 1.0 - base_mass)
    if residual > config.tail_tolerance:
        raise ModelError("score-grid hard cap cannot satisfy tail tolerance")
    matrix: list[list[float]] = []
    for home_goals, home_probability in enumerate(home_probabilities):
        row: list[float] = []
        for away_goals, away_probability in enumerate(away_probabilities):
            factor = dixon_coles_correction(
                home_goals=home_goals,
                away_goals=away_goals,
                home_intensity=home_intensity,
                away_intensity=away_intensity,
                rho=rho,
            )
            row.append(home_probability * away_probability * factor)
        matrix.append(row)
    mass = math.fsum(value for row in matrix for value in row)
    if not math.isfinite(mass) or mass <= 0.0:
        raise ModelError("score distribution has invalid probability mass")
    normalized = tuple(tuple(value / mass for value in row) for row in matrix)
    return JointScoreDistribution(
        probabilities=normalized,
        home_intensity=home_intensity,
        away_intensity=away_intensity,
        rho=rho,
        score_grid_maximum=maximum,
        residual_tail_mass=residual,
        tail_tolerance=config.tail_tolerance,
        model_family=model_family,
        model_version=FOOTBALL_SCORE_MODEL_VERSION,
        competition_id=competition_id,
        prediction_cutoff=prediction_cutoff,
        fallback_used=fallback_used,
    )


def dixon_coles_correction(
    *,
    home_goals: int,
    away_goals: int,
    home_intensity: float,
    away_intensity: float,
    rho: float,
) -> float:
    """Return the reviewed Dixon-Coles correction for one score state."""
    if (home_goals, away_goals) == (0, 0):
        factor = 1.0 - (home_intensity * away_intensity * rho)
    elif (home_goals, away_goals) == (0, 1):
        factor = 1.0 + (home_intensity * rho)
    elif (home_goals, away_goals) == (1, 0):
        factor = 1.0 + (away_intensity * rho)
    elif (home_goals, away_goals) == (1, 1):
        factor = 1.0 - rho
    else:
        factor = 1.0
    if not math.isfinite(factor) or factor <= 0.0:
        raise ModelError("Dixon-Coles correction factor must be finite and positive")
    return factor


def temperature_scale_distribution(
    distribution: JointScoreDistribution,
    *,
    temperature: float,
) -> JointScoreDistribution:
    """Apply one coherence-preserving global temperature to the full matrix."""
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise ModelError("calibration temperature must be finite and positive")
    exponent = 1.0 / temperature
    powered = tuple(tuple(value**exponent for value in row) for row in distribution.probabilities)
    total = math.fsum(value for row in powered for value in row)
    normalized = tuple(tuple(value / total for value in row) for row in powered)
    return replace(
        distribution,
        probabilities=normalized,
        calibration_method=f"global-temperature:{temperature:.12g}",
    )


def rake_result_regions(
    distribution: JointScoreDistribution,
    *,
    home_probability: float,
    draw_probability: float,
    away_probability: float,
) -> JointScoreDistribution:
    """Rake home/draw/away regions while preserving within-region score ratios."""
    targets = (home_probability, draw_probability, away_probability)
    if any(not math.isfinite(value) or value < 0.0 for value in targets):
        raise ModelError("raking targets must be finite and non-negative")
    if not math.isclose(math.fsum(targets), 1.0, rel_tol=0.0, abs_tol=1e-10):
        raise ModelError("raking targets must sum to one")
    bases = [0.0, 0.0, 0.0]
    for home_goals, row in enumerate(distribution.probabilities):
        for away_goals, value in enumerate(row):
            region = 0 if home_goals > away_goals else 1 if home_goals == away_goals else 2
            bases[region] += value
    if any(base <= 0.0 and target > 0.0 for base, target in zip(bases, targets, strict=True)):
        raise ModelError("cannot rake positive target mass into an empty score region")
    scaled: list[tuple[float, ...]] = []
    for home_goals, row in enumerate(distribution.probabilities):
        values: list[float] = []
        for away_goals, value in enumerate(row):
            region = 0 if home_goals > away_goals else 1 if home_goals == away_goals else 2
            values.append(0.0 if bases[region] == 0.0 else value * targets[region] / bases[region])
        scaled.append(tuple(values))
    total = math.fsum(value for row in scaled for value in row)
    normalized = tuple(tuple(value / total for value in row) for row in scaled)
    return replace(
        distribution,
        probabilities=normalized,
        calibration_method="result-region-raking",
    )


def score_model_to_payload(model: FootballScoreModel) -> dict[str, JsonValue]:
    """Serialize one fitted model to an exact canonical JSON payload."""
    config = model.configuration
    diagnostics = model.diagnostics
    return {
        "model_family": model.model_family,
        "model_version": FOOTBALL_SCORE_MODEL_VERSION,
        "competition_id": model.competition_id,
        "training_start": model.training_start.isoformat(),
        "training_end": model.training_end.isoformat(),
        "configuration": {
            "decay_per_day": config.decay_per_day,
            "l2_regularization": config.l2_regularization,
            "learning_rate": config.learning_rate,
            "maximum_iterations": config.maximum_iterations,
            "convergence_tolerance": config.convergence_tolerance,
            "minimum_matches": config.minimum_matches,
            "minimum_grid_goals": config.minimum_grid_goals,
            "maximum_grid_goals": config.maximum_grid_goals,
            "tail_tolerance": config.tail_tolerance,
            "rho_minimum": config.rho_minimum,
            "rho_maximum": config.rho_maximum,
            "rho_grid_points": config.rho_grid_points,
        },
        "parameters": {
            "base_log_rate": model.base_log_rate,
            "home_advantage": model.home_advantage,
            "rho": model.rho,
            "teams": [
                {
                    "team_id": team,
                    "attack": model.attack_strengths[index],
                    "defence": model.defence_strengths[index],
                }
                for index, team in enumerate(model.teams)
            ],
        },
        "diagnostics": {
            "converged": diagnostics.converged,
            "iterations": diagnostics.iterations,
            "objective": diagnostics.objective,
            "gradient_norm": diagnostics.gradient_norm,
            "rho_candidates_evaluated": diagnostics.rho_candidates_evaluated,
            "failure_reason": diagnostics.failure_reason,
        },
        "identifiability": "sum-to-zero-attack-and-defence",
        "unseen_team_policy": "competition-average-zero-effect",
    }


def score_model_from_payload(payload: object) -> FootballScoreModel:
    """Strictly reload a score model without arbitrary object deserialization."""
    if not isinstance(payload, dict) or set(payload) != {
        "model_family",
        "model_version",
        "competition_id",
        "training_start",
        "training_end",
        "configuration",
        "parameters",
        "diagnostics",
        "identifiability",
        "unseen_team_policy",
    }:
        raise ArtifactError("football score model payload fields are not exact")
    if payload["model_version"] != FOOTBALL_SCORE_MODEL_VERSION:
        raise ArtifactError("unsupported football score model version")
    if payload["identifiability"] != "sum-to-zero-attack-and-defence":
        raise ArtifactError("football score model identifiability policy mismatch")
    if payload["unseen_team_policy"] != "competition-average-zero-effect":
        raise ArtifactError("football score model unseen-team policy mismatch")
    configuration = _configuration_from_json(payload["configuration"])
    parameters = _exact_dict(
        payload["parameters"],
        {"base_log_rate", "home_advantage", "rho", "teams"},
        "parameters",
    )
    diagnostics_raw = _exact_dict(
        payload["diagnostics"],
        {
            "converged",
            "iterations",
            "objective",
            "gradient_norm",
            "rho_candidates_evaluated",
            "failure_reason",
        },
        "diagnostics",
    )
    team_rows = parameters["teams"]
    if not isinstance(team_rows, list) or not team_rows:
        raise ArtifactError("parameters.teams must be a non-empty JSON array")
    teams: list[str] = []
    attacks: list[float] = []
    defences: list[float] = []
    for index, raw in enumerate(team_rows):
        row = _exact_dict(raw, {"team_id", "attack", "defence"}, f"teams[{index}]")
        teams.append(_string(row["team_id"], f"teams[{index}].team_id"))
        attacks.append(_finite(row["attack"], f"teams[{index}].attack"))
        defences.append(_finite(row["defence"], f"teams[{index}].defence"))
    try:
        training_start = date.fromisoformat(_string(payload["training_start"], "training_start"))
        training_end = date.fromisoformat(_string(payload["training_end"], "training_end"))
    except ValueError as exc:
        raise ArtifactError("score model training dates are invalid") from exc
    diagnostics = ScoreModelDiagnostics(
        converged=_boolean(diagnostics_raw["converged"], "diagnostics.converged"),
        iterations=_integer(diagnostics_raw["iterations"], "diagnostics.iterations"),
        objective=_finite(diagnostics_raw["objective"], "diagnostics.objective"),
        gradient_norm=_finite(diagnostics_raw["gradient_norm"], "diagnostics.gradient_norm"),
        rho_candidates_evaluated=_integer(
            diagnostics_raw["rho_candidates_evaluated"],
            "diagnostics.rho_candidates_evaluated",
        ),
        failure_reason=_optional_string(
            diagnostics_raw["failure_reason"],
            "diagnostics.failure_reason",
        ),
    )
    try:
        return FootballScoreModel(
            model_family=_string(payload["model_family"], "model_family"),
            competition_id=_string(payload["competition_id"], "competition_id"),
            training_start=training_start,
            training_end=training_end,
            teams=tuple(teams),
            base_log_rate=_finite(parameters["base_log_rate"], "parameters.base_log_rate"),
            home_advantage=_finite(parameters["home_advantage"], "parameters.home_advantage"),
            attack_strengths=tuple(attacks),
            defence_strengths=tuple(defences),
            rho=_finite(parameters["rho"], "parameters.rho"),
            configuration=configuration,
            diagnostics=diagnostics,
        )
    except ModelError as exc:
        raise ArtifactError(str(exc)) from exc


def write_score_model_artifact(
    *,
    root: object,
    relative_directory: str,
    model: FootballScoreModel,
) -> AnalyticalArtifact:
    """Publish a content-addressed immutable score-model artifact."""
    from pathlib import Path

    if not isinstance(root, Path):
        raise ArtifactError("score model artifact root must be a Path")
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_SCORE_ARTIFACT_TYPE,
        schema_version=FOOTBALL_SCORE_ARTIFACT_SCHEMA,
        payload=score_model_to_payload(model),
    )


def load_score_model_artifact(
    *,
    root: object,
    relative_directory: str,
    expected_checksum: str | None = None,
    expected_artifact_id: str | None = None,
) -> tuple[AnalyticalArtifact, FootballScoreModel]:
    """Strictly verify and reload a safe score-model artifact."""
    from pathlib import Path

    if not isinstance(root, Path):
        raise ArtifactError("score model artifact root must be a Path")
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_SCORE_ARTIFACT_TYPE,
        expected_schema_version=FOOTBALL_SCORE_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
        expected_artifact_id=expected_artifact_id,
    )
    return artifact, score_model_from_payload(artifact.payload)


def _fit_base_model(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    configuration: ScoreModelConfiguration,
    model_family: str,
) -> FootballScoreModel:
    ordered = _validate_matches(matches, configuration)
    competition = ordered[0].competition_id
    teams = tuple(
        sorted({item.home_team_id for item in ordered} | {item.away_team_id for item in ordered})
    )
    team_index = {team: index for index, team in enumerate(teams)}
    weights = _time_weights(ordered, decay_per_day=configuration.decay_per_day)
    mean_goals = max(
        0.05,
        math.fsum(
            weight * (match.home_goals + match.away_goals) / 2.0
            for match, weight in zip(ordered, weights, strict=True)
        )
        / math.fsum(weights),
    )
    base = math.log(mean_goals)
    home_advantage = 0.1
    attack = np.zeros(len(teams), dtype=np.float64)
    defence = np.zeros(len(teams), dtype=np.float64)
    converged = False
    gradient_norm = math.inf
    objective = math.inf
    iteration = 0
    step = configuration.learning_rate
    for _iteration in range(1, configuration.maximum_iterations + 1):
        iteration = _iteration
        (
            objective,
            gradient_base,
            gradient_home,
            gradient_attack,
            gradient_defence,
        ) = _poisson_objective_and_gradient(
            ordered,
            weights,
            team_index,
            base,
            home_advantage,
            attack,
            defence,
            configuration.l2_regularization,
        )
        gradient_norm = float(
            math.sqrt(
                (gradient_base * gradient_base)
                + (gradient_home * gradient_home)
                + float(np.dot(gradient_attack, gradient_attack))
                + float(np.dot(gradient_defence, gradient_defence))
            )
        )
        if not math.isfinite(objective) or not math.isfinite(gradient_norm):
            raise ModelError("Poisson optimizer produced a non-finite state")
        gradient_attack -= float(np.mean(gradient_attack))
        gradient_defence -= float(np.mean(gradient_defence))
        candidate_base = float(np.clip(base - step * gradient_base, -3.0, 2.5))
        candidate_home = float(np.clip(home_advantage - step * gradient_home, -1.5, 1.5))
        candidate_attack = np.clip(attack - step * gradient_attack, -2.5, 2.5)
        candidate_defence = np.clip(defence - step * gradient_defence, -2.5, 2.5)
        candidate_attack -= float(np.mean(candidate_attack))
        candidate_defence -= float(np.mean(candidate_defence))
        candidate_objective = _poisson_objective_and_gradient(
            ordered,
            weights,
            team_index,
            candidate_base,
            candidate_home,
            candidate_attack,
            candidate_defence,
            configuration.l2_regularization,
        )[0]
        if candidate_objective <= objective:
            improvement = objective - candidate_objective
            base = candidate_base
            home_advantage = candidate_home
            attack = candidate_attack
            defence = candidate_defence
            objective = candidate_objective
            if improvement <= configuration.convergence_tolerance * max(1.0, abs(objective)):
                converged = True
                break
            step = min(configuration.learning_rate, step * 1.05)
        else:
            step *= 0.5
            if step < 1e-12:
                break
    if not converged:
        raise ModelError(
            "Poisson optimizer did not converge within "
            f"{configuration.maximum_iterations} iterations"
        )
    diagnostics = ScoreModelDiagnostics(
        converged=True,
        iterations=iteration,
        objective=objective,
        gradient_norm=gradient_norm,
        rho_candidates_evaluated=0,
    )
    return FootballScoreModel(
        model_family=model_family,
        competition_id=competition,
        training_start=ordered[0].event_date,
        training_end=ordered[-1].event_date,
        teams=teams,
        base_log_rate=base,
        home_advantage=home_advantage,
        attack_strengths=tuple(float(value) for value in attack),
        defence_strengths=tuple(float(value) for value in defence),
        rho=0.0,
        configuration=configuration,
        diagnostics=diagnostics,
    )


def _validate_matches(
    matches: tuple[ScoreTrainingMatch, ...],
    configuration: ScoreModelConfiguration,
) -> tuple[ScoreTrainingMatch, ...]:
    if len(matches) < configuration.minimum_matches:
        raise ModelError(
            f"score-model training requires at least {configuration.minimum_matches} matches"
        )
    competitions = {item.competition_id for item in matches}
    if len(competitions) != 1:
        raise ModelError("one score model cannot mix competitions")
    by_id: dict[str, ScoreTrainingMatch] = {}
    for match in matches:
        previous = by_id.get(match.canonical_event_id)
        if previous is not None and previous != match:
            raise ModelError("conflicting duplicate score training identity")
        by_id[match.canonical_event_id] = match
    ordered = tuple(
        sorted(by_id.values(), key=lambda item: (item.event_date, item.canonical_event_id))
    )
    if len(ordered) < configuration.minimum_matches:
        raise ModelError("deduplicated score-model sample is too small")
    return ordered


def _time_weights(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    decay_per_day: float,
) -> np.ndarray:
    latest = matches[-1].event_date
    values = np.asarray(
        [math.exp(-decay_per_day * (latest - item.event_date).days) for item in matches],
        dtype=np.float64,
    )
    return values / float(np.mean(values))


def _poisson_objective_and_gradient(
    matches: tuple[ScoreTrainingMatch, ...],
    weights: np.ndarray,
    team_index: dict[str, int],
    base: float,
    home_advantage: float,
    attack: np.ndarray,
    defence: np.ndarray,
    l2: float,
) -> tuple[float, float, float, np.ndarray, np.ndarray]:
    gradient_attack = np.zeros_like(attack)
    gradient_defence = np.zeros_like(defence)
    gradient_base = 0.0
    gradient_home = 0.0
    objective = 0.0
    total_weight = float(np.sum(weights))
    for match, weight in zip(matches, weights, strict=True):
        home = team_index[match.home_team_id]
        away = team_index[match.away_team_id]
        eta_home = base + home_advantage + attack[home] - defence[away]
        eta_away = base + attack[away] - defence[home]
        lambda_home = math.exp(eta_home)
        lambda_away = math.exp(eta_away)
        residual_home = lambda_home - match.home_goals
        residual_away = lambda_away - match.away_goals
        objective += weight * (
            lambda_home
            - (match.home_goals * eta_home)
            + lambda_away
            - (match.away_goals * eta_away)
        )
        gradient_base += weight * (residual_home + residual_away)
        gradient_home += weight * residual_home
        gradient_attack[home] += weight * residual_home
        gradient_attack[away] += weight * residual_away
        gradient_defence[away] -= weight * residual_home
        gradient_defence[home] -= weight * residual_away
    objective /= total_weight
    gradient_base /= total_weight
    gradient_home /= total_weight
    gradient_attack /= total_weight
    gradient_defence /= total_weight
    objective += 0.5 * l2 * (float(np.dot(attack, attack)) + float(np.dot(defence, defence)))
    gradient_attack += l2 * attack
    gradient_defence += l2 * defence
    return (
        float(objective),
        float(gradient_base),
        float(gradient_home),
        gradient_attack,
        gradient_defence,
    )


def _rho_objective(
    model: FootballScoreModel,
    matches: tuple[ScoreTrainingMatch, ...],
    weights: np.ndarray,
    rho: float,
) -> float:
    objective = 0.0
    for match, weight in zip(matches, weights, strict=True):
        home, away, _ = model.intensities(
            home_team_id=match.home_team_id,
            away_team_id=match.away_team_id,
        )
        try:
            factor = dixon_coles_correction(
                home_goals=match.home_goals,
                away_goals=match.away_goals,
                home_intensity=home,
                away_intensity=away,
                rho=rho,
            )
        except ModelError:
            return math.inf
        objective -= weight * math.log(factor)
    return float(objective / float(np.sum(weights)))


def _poisson_probabilities(intensity: float, maximum: int) -> tuple[float, ...]:
    values = [math.exp(-intensity)]
    for score in range(1, maximum + 1):
        values.append(values[-1] * intensity / float(score))
    return tuple(values)


def _select_grid_maximum(
    home_intensity: float,
    away_intensity: float,
    configuration: ScoreModelConfiguration,
) -> int:
    for maximum in range(
        configuration.minimum_grid_goals,
        configuration.maximum_grid_goals + 1,
    ):
        home_mass = math.fsum(_poisson_probabilities(home_intensity, maximum))
        away_mass = math.fsum(_poisson_probabilities(away_intensity, maximum))
        if 1.0 - (home_mass * away_mass) <= configuration.tail_tolerance:
            return maximum
    raise ModelError("score-grid hard cap cannot satisfy tail tolerance")


def _configuration_from_json(value: object) -> ScoreModelConfiguration:
    raw = _exact_dict(
        value,
        {
            "decay_per_day",
            "l2_regularization",
            "learning_rate",
            "maximum_iterations",
            "convergence_tolerance",
            "minimum_matches",
            "minimum_grid_goals",
            "maximum_grid_goals",
            "tail_tolerance",
            "rho_minimum",
            "rho_maximum",
            "rho_grid_points",
        },
        "configuration",
    )
    try:
        return ScoreModelConfiguration(
            decay_per_day=_finite(raw["decay_per_day"], "configuration.decay_per_day"),
            l2_regularization=_finite(
                raw["l2_regularization"],
                "configuration.l2_regularization",
            ),
            learning_rate=_finite(raw["learning_rate"], "configuration.learning_rate"),
            maximum_iterations=_integer(
                raw["maximum_iterations"],
                "configuration.maximum_iterations",
            ),
            convergence_tolerance=_finite(
                raw["convergence_tolerance"],
                "configuration.convergence_tolerance",
            ),
            minimum_matches=_integer(raw["minimum_matches"], "configuration.minimum_matches"),
            minimum_grid_goals=_integer(
                raw["minimum_grid_goals"],
                "configuration.minimum_grid_goals",
            ),
            maximum_grid_goals=_integer(
                raw["maximum_grid_goals"],
                "configuration.maximum_grid_goals",
            ),
            tail_tolerance=_finite(raw["tail_tolerance"], "configuration.tail_tolerance"),
            rho_minimum=_finite(raw["rho_minimum"], "configuration.rho_minimum"),
            rho_maximum=_finite(raw["rho_maximum"], "configuration.rho_maximum"),
            rho_grid_points=_integer(
                raw["rho_grid_points"],
                "configuration.rho_grid_points",
            ),
        )
    except ModelError as exc:
        raise ArtifactError(str(exc)) from exc


def _exact_dict(value: object, fields: set[str], description: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ArtifactError(f"{description} fields are not exact")
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ArtifactError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ArtifactError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ArtifactError(f"{field} must be finite")
    return number


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ArtifactError(f"{field} must be a non-negative integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ArtifactError(f"{field} must be a boolean")
    return value

"""Leakage-safe covariate score challenger and coherent surface ensemble."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Final

from sports_analytics.artifact_strict import (
    require_dict,
    require_list,
    require_sha256_checksum,
    require_str,
)
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    build_analytical_artifact_document,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.types import JsonValue
from sports_analytics.models.football_scores import (
    FootballScoreModel,
    JointScoreDistribution,
    ScoreModelConfiguration,
    ScoreTrainingMatch,
    fit_dixon_coles,
    joint_score_from_intensities,
)

COVARIATE_FEATURE_VERSION: Final[str] = "football-score-form-covariates-v1"
CHALLENGER_ARTIFACT_TYPE: Final[str] = "football-score-challengers"
CHALLENGER_ARTIFACT_SCHEMA: Final[str] = "football-score-challengers-v2"


@dataclass(frozen=True, slots=True)
class CovariateScoreConfiguration:
    """Small reviewed pre-kickoff feature set."""

    recent_match_window: int = 5
    attack_form_coefficient: float = 0.03
    defence_form_coefficient: float = 0.02
    maximum_absolute_adjustment: float = 0.1
    feature_version: str = COVARIATE_FEATURE_VERSION
    missing_value_policy: str = "competition-mean-zero-after-scaling"
    scaling_policy: str = "cross-team-z-score-at-training-cutoff"

    def __post_init__(self) -> None:
        if type(self.recent_match_window) is not int or not 2 <= self.recent_match_window <= 20:
            raise ModelError("recent_match_window must lie in [2, 20]")
        for field in (
            "attack_form_coefficient",
            "defence_form_coefficient",
            "maximum_absolute_adjustment",
        ):
            value = getattr(self, field)
            if not math.isfinite(value) or value < 0.0:
                raise ModelError(f"{field} must be finite and non-negative")
        if self.feature_version != COVARIATE_FEATURE_VERSION:
            raise ModelError("unsupported covariate feature version")


@dataclass(frozen=True, slots=True)
class CovariateScoreModel:
    base_model: FootballScoreModel
    configuration: CovariateScoreConfiguration
    attack_form: tuple[float, ...]
    defence_form: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.attack_form) != len(self.base_model.teams) or len(self.defence_form) != len(
            self.base_model.teams
        ):
            raise ModelError("covariate form vectors must align with base model teams")
        if any(not math.isfinite(value) for value in (*self.attack_form, *self.defence_form)):
            raise ModelError("covariate form values must be finite")


def fit_covariate_dixon_coles(
    matches: tuple[ScoreTrainingMatch, ...],
    *,
    score_configuration: ScoreModelConfiguration | None = None,
    configuration: CovariateScoreConfiguration | None = None,
) -> CovariateScoreModel:
    rules = configuration or CovariateScoreConfiguration()
    base = fit_dixon_coles(matches, configuration=score_configuration)
    attack, defence = _scaled_recent_form(matches, base.teams, rules.recent_match_window)
    return CovariateScoreModel(base, rules, attack, defence)


def predict_covariate_score(
    model: CovariateScoreModel,
    *,
    home_team_id: str,
    away_team_id: str,
    prediction_cutoff: date,
) -> JointScoreDistribution:
    if prediction_cutoff <= model.base_model.training_end:
        raise ModelError("prediction cutoff must follow covariate training")
    home, away, fallback = model.base_model.intensities(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )
    indexes = {team: index for index, team in enumerate(model.base_model.teams)}
    home_index = indexes.get(home_team_id)
    away_index = indexes.get(away_team_id)
    home_adjustment = _bounded_adjustment(
        model,
        attack=(0.0 if home_index is None else model.attack_form[home_index]),
        opposing_defence=(0.0 if away_index is None else model.defence_form[away_index]),
    )
    away_adjustment = _bounded_adjustment(
        model,
        attack=(0.0 if away_index is None else model.attack_form[away_index]),
        opposing_defence=(0.0 if home_index is None else model.defence_form[home_index]),
    )
    surface = joint_score_from_intensities(
        home_intensity=home * math.exp(home_adjustment),
        away_intensity=away * math.exp(away_adjustment),
        rho=model.base_model.rho,
        model_family=model.base_model.model_family,
        competition_id=model.base_model.competition_id,
        prediction_cutoff=prediction_cutoff,
        configuration=model.base_model.configuration,
        fallback_used=fallback,
    )
    return replace(
        surface,
        model_family="covariate-dixon-coles",
        model_version=COVARIATE_FEATURE_VERSION,
    )


def select_ensemble_weights(
    component_surfaces: tuple[tuple[JointScoreDistribution, ...], ...],
    observed_scores: tuple[tuple[int, int], ...],
    *,
    weight_step: float = 0.1,
) -> tuple[float, ...]:
    """Finite deterministic calibration-only grid for two or three components."""
    if len(component_surfaces) not in {2, 3}:
        raise ModelError("ensemble requires two or three complete surface components")
    if any(len(rows) != len(observed_scores) for rows in component_surfaces):
        raise ModelError("ensemble calibration rows are not aligned")
    if not math.isclose(weight_step, 0.1, abs_tol=1e-12):
        raise ModelError("only the reviewed 0.1 ensemble grid is supported")
    units = 10
    candidates: list[tuple[float, tuple[float, ...]]] = []
    for first in range(units + 1):
        for second in range(units - first + 1):
            if len(component_surfaces) == 2:
                if first + second != units:
                    continue
                weights: tuple[float, ...] = (first / units, second / units)
            else:
                third = units - first - second
                weights = (first / units, second / units, third / units)
            loss = 0.0
            for index, (home_goals, away_goals) in enumerate(observed_scores):
                probability = math.fsum(
                    weight
                    * component_surfaces[component][index].probability(
                        home_goals,
                        away_goals,
                    )
                    for component, weight in enumerate(weights)
                )
                loss -= math.log(max(probability, 1e-15))
            candidates.append((loss / len(observed_scores), weights))
    return min(candidates, key=lambda item: (item[0], item[1]))[1]


def blend_score_surfaces(
    surfaces: tuple[JointScoreDistribution, ...],
    *,
    weights: tuple[float, ...],
) -> JointScoreDistribution:
    if len(surfaces) != len(weights) or not surfaces:
        raise ModelError("ensemble surface and weight counts must match")
    if any(not math.isfinite(value) or value < 0.0 for value in weights) or not math.isclose(
        math.fsum(weights), 1.0, abs_tol=1e-12
    ):
        raise ModelError("ensemble weights must be non-negative and sum to one")
    maximum = max(item.score_grid_maximum for item in surfaces)
    matrix = tuple(
        tuple(
            math.fsum(
                weight * surface.probability(home, away)
                for weight, surface in zip(weights, surfaces, strict=True)
            )
            for away in range(maximum + 1)
        )
        for home in range(maximum + 1)
    )
    mass = math.fsum(value for row in matrix for value in row)
    normalized = tuple(tuple(value / mass for value in row) for row in matrix)
    first = surfaces[0]
    return JointScoreDistribution(
        probabilities=normalized,
        home_intensity=math.fsum(
            weight * surface.home_intensity
            for weight, surface in zip(weights, surfaces, strict=True)
        ),
        away_intensity=math.fsum(
            weight * surface.away_intensity
            for weight, surface in zip(weights, surfaces, strict=True)
        ),
        rho=math.fsum(
            weight * surface.rho for weight, surface in zip(weights, surfaces, strict=True)
        ),
        score_grid_maximum=maximum,
        residual_tail_mass=math.fsum(
            weight * surface.residual_tail_mass
            for weight, surface in zip(weights, surfaces, strict=True)
        ),
        tail_tolerance=max(item.tail_tolerance for item in surfaces),
        model_family="coherent-score-ensemble",
        model_version="football-score-ensemble-v1",
        competition_id=first.competition_id,
        prediction_cutoff=first.prediction_cutoff,
        fallback_used=any(item.fallback_used for item in surfaces),
        calibration_method="complete-surface-convex-blend",
    )


def write_challenger_artifact(
    *,
    root: Path,
    relative_directory: str,
    model: CovariateScoreModel,
    ensemble_weights: tuple[float, ...],
    training_evidence_artifact_id: str,
    training_evidence_checksum_sha256: str,
    source_snapshot_refs: tuple[tuple[str, str], ...],
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=CHALLENGER_ARTIFACT_TYPE,
        schema_version=CHALLENGER_ARTIFACT_SCHEMA,
        payload=_challenger_payload(
            model,
            ensemble_weights,
            training_evidence_artifact_id=training_evidence_artifact_id,
            training_evidence_checksum_sha256=training_evidence_checksum_sha256,
            source_snapshot_refs=source_snapshot_refs,
        ),
    )


def challenger_artifact_id(
    *,
    model: CovariateScoreModel,
    ensemble_weights: tuple[float, ...],
    training_evidence_artifact_id: str,
    training_evidence_checksum_sha256: str,
    source_snapshot_refs: tuple[tuple[str, str], ...],
) -> str:
    """Return the exact immutable identity expected from a challenger publication."""
    document = build_analytical_artifact_document(
        artifact_type=CHALLENGER_ARTIFACT_TYPE,
        schema_version=CHALLENGER_ARTIFACT_SCHEMA,
        payload=_challenger_payload(
            model,
            ensemble_weights,
            training_evidence_artifact_id=training_evidence_artifact_id,
            training_evidence_checksum_sha256=training_evidence_checksum_sha256,
            source_snapshot_refs=source_snapshot_refs,
        ),
    )
    return require_str(document["artifact_id"], field="artifact_id")


def load_challenger_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=CHALLENGER_ARTIFACT_TYPE,
        expected_schema_version=CHALLENGER_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "feature_version",
        "temporal_semantics",
        "missing_value_policy",
        "scaling_policy",
        "recent_match_window",
        "attack_form_coefficient",
        "defence_form_coefficient",
        "maximum_absolute_adjustment",
        "base_model_family",
        "teams",
        "ensemble_weights",
        "training_evidence",
        "source_snapshot_refs",
    }:
        raise ArtifactError("football challenger artifact fields are not exact")
    weights = payload["ensemble_weights"]
    if not isinstance(weights, list) or not weights:
        raise ArtifactError("football challenger ensemble weights are invalid")
    numeric_weights = tuple(
        float(value)
        for value in weights
        if not isinstance(value, bool) and isinstance(value, int | float)
    )
    if len(numeric_weights) != len(weights) or not math.isclose(
        math.fsum(numeric_weights),
        1.0,
        abs_tol=1e-12,
    ):
        raise ArtifactError("football challenger ensemble weights are invalid")
    if payload["feature_version"] != COVARIATE_FEATURE_VERSION:
        raise ArtifactError("football challenger feature version is invalid")
    training_evidence = require_dict(
        payload["training_evidence"],
        field="training_evidence",
    )
    if set(training_evidence) != {"artifact_id", "checksum_sha256"}:
        raise ArtifactError("football challenger training evidence fields are not exact")
    require_sha256_checksum(
        training_evidence["artifact_id"],
        field="training_evidence.artifact_id",
    )
    require_sha256_checksum(
        training_evidence["checksum_sha256"],
        field="training_evidence.checksum_sha256",
    )
    snapshot_refs = require_list(payload["source_snapshot_refs"], field="source_snapshot_refs")
    parsed_refs: list[tuple[str, str]] = []
    for index, raw in enumerate(snapshot_refs):
        item = require_dict(raw, field=f"source_snapshot_refs[{index}]")
        if set(item) != {"snapshot_id", "checksum_sha256"}:
            raise ArtifactError("football challenger snapshot reference fields are not exact")
        parsed_refs.append(
            (
                require_str(
                    item["snapshot_id"],
                    field=f"source_snapshot_refs[{index}].snapshot_id",
                ),
                require_sha256_checksum(
                    item["checksum_sha256"],
                    field=f"source_snapshot_refs[{index}].checksum_sha256",
                ),
            )
        )
    if not parsed_refs or parsed_refs != sorted(set(parsed_refs)):
        raise ArtifactError("football challenger snapshot references are not canonical")
    return artifact


def _scaled_recent_form(
    matches: tuple[ScoreTrainingMatch, ...],
    teams: tuple[str, ...],
    window: int,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    scored: dict[str, list[int]] = {team: [] for team in teams}
    conceded: dict[str, list[int]] = {team: [] for team in teams}
    for match in sorted(matches, key=lambda item: (item.event_date, item.canonical_event_id)):
        scored[match.home_team_id].append(match.home_goals)
        conceded[match.home_team_id].append(match.away_goals)
        scored[match.away_team_id].append(match.away_goals)
        conceded[match.away_team_id].append(match.home_goals)
    attack_raw = tuple(
        float(sum(scored[team][-window:]) / len(scored[team][-window:])) for team in teams
    )
    defence_raw = tuple(
        float(sum(conceded[team][-window:]) / len(conceded[team][-window:])) for team in teams
    )
    return _zscore(attack_raw), _zscore(defence_raw)


def _zscore(values: tuple[float, ...]) -> tuple[float, ...]:
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
    scale = math.sqrt(variance)
    if scale <= 1e-12:
        return tuple(0.0 for _ in values)
    return tuple((value - mean) / scale for value in values)


def _bounded_adjustment(
    model: CovariateScoreModel,
    *,
    attack: float,
    opposing_defence: float,
) -> float:
    raw = (
        model.configuration.attack_form_coefficient * attack
        + model.configuration.defence_form_coefficient * opposing_defence
    )
    bound = model.configuration.maximum_absolute_adjustment
    return max(-bound, min(bound, raw))


def _challenger_payload(
    model: CovariateScoreModel,
    ensemble_weights: tuple[float, ...],
    *,
    training_evidence_artifact_id: str,
    training_evidence_checksum_sha256: str,
    source_snapshot_refs: tuple[tuple[str, str], ...],
) -> dict[str, JsonValue]:
    evidence_id = require_sha256_checksum(
        training_evidence_artifact_id,
        field="training_evidence_artifact_id",
    )
    evidence_checksum = require_sha256_checksum(
        training_evidence_checksum_sha256,
        field="training_evidence_checksum_sha256",
    )
    canonical_snapshot_refs = tuple(sorted(set(source_snapshot_refs)))
    if not canonical_snapshot_refs or canonical_snapshot_refs != source_snapshot_refs:
        raise ArtifactError("source snapshot references must be non-empty, unique, and sorted")
    validated_snapshot_refs: list[JsonValue] = [
        {
            "snapshot_id": require_str(snapshot_id, field="source_snapshot_refs[].snapshot_id"),
            "checksum_sha256": require_sha256_checksum(
                checksum,
                field="source_snapshot_refs[].checksum_sha256",
            ),
        }
        for snapshot_id, checksum in canonical_snapshot_refs
    ]
    return {
        "feature_version": model.configuration.feature_version,
        "temporal_semantics": "last-completed-matches-at-training-cutoff-only",
        "missing_value_policy": model.configuration.missing_value_policy,
        "scaling_policy": model.configuration.scaling_policy,
        "recent_match_window": model.configuration.recent_match_window,
        "attack_form_coefficient": model.configuration.attack_form_coefficient,
        "defence_form_coefficient": model.configuration.defence_form_coefficient,
        "maximum_absolute_adjustment": model.configuration.maximum_absolute_adjustment,
        "base_model_family": model.base_model.model_family,
        "teams": [
            {
                "team_id": team,
                "attack_form": model.attack_form[index],
                "defence_form": model.defence_form[index],
            }
            for index, team in enumerate(model.base_model.teams)
        ],
        "ensemble_weights": list(ensemble_weights),
        "training_evidence": {
            "artifact_id": evidence_id,
            "checksum_sha256": evidence_checksum,
        },
        "source_snapshot_refs": validated_snapshot_refs,
    }

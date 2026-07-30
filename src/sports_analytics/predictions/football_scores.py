"""Strict persisted coherent football probability and fair-odds artifacts."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.types import JsonValue
from sports_analytics.markets.football_score_markets import (
    FootballMarketProbability,
    derive_full_time_markets,
)
from sports_analytics.models.football_scores import JointScoreDistribution

FOOTBALL_PROBABILITY_ARTIFACT_TYPE: Final[str] = "football-probability-surface"
FOOTBALL_PROBABILITY_ARTIFACT_SCHEMA: Final[str] = "football-probability-surface-v1"


def probability_surface_payload(
    *,
    canonical_event_id: str,
    model_artifact_id: str,
    distribution: JointScoreDistribution,
    markets: tuple[FootballMarketProbability, ...] | None = None,
) -> dict[str, JsonValue]:
    """Serialize one bounded coherent score surface and all derived fair odds."""
    derived = markets or derive_full_time_markets(distribution)
    return {
        "canonical_event_id": canonical_event_id,
        "model_artifact_id": model_artifact_id,
        "prediction_cutoff": distribution.prediction_cutoff.isoformat(),
        "model_family": distribution.model_family,
        "model_version": distribution.model_version,
        "competition_id": distribution.competition_id,
        "calibration_method": distribution.calibration_method,
        "home_intensity": distribution.home_intensity,
        "away_intensity": distribution.away_intensity,
        "rho": distribution.rho,
        "score_grid_maximum": distribution.score_grid_maximum,
        "residual_tail_mass": distribution.residual_tail_mass,
        "tail_tolerance": distribution.tail_tolerance,
        "fallback_used": distribution.fallback_used,
        "joint_score_probabilities": [
            {
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": probability,
            }
            for home_goals, row in enumerate(distribution.probabilities)
            for away_goals, probability in enumerate(row)
        ],
        "markets": [
            {
                "market_family": item.market_family,
                "market_key": item.market_key,
                "outcome_key": item.outcome_key,
                "market_period": item.market_period,
                "participant_scope": item.participant_scope,
                "line_value": (None if item.line_value is None else format(item.line_value, "f")),
                "probability": item.probability,
                "fair_decimal_odds": item.fair_decimal_odds,
                "push_probability": item.push_probability,
                "limitation": item.limitation,
            }
            for item in derived
        ],
        "price_semantics": {
            "probability": "model-probability",
            "fair_decimal_odds": "model-estimate-not-offered-price",
            "offered_decimal_odds": None,
            "expected_value": None,
        },
        "capability_state": "fair-odds-only",
    }


def write_football_probability_artifact(
    *,
    root: Path,
    relative_directory: str,
    canonical_event_id: str,
    model_artifact_id: str,
    distribution: JointScoreDistribution,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_PROBABILITY_ARTIFACT_TYPE,
        schema_version=FOOTBALL_PROBABILITY_ARTIFACT_SCHEMA,
        payload=probability_surface_payload(
            canonical_event_id=canonical_event_id,
            model_artifact_id=model_artifact_id,
            distribution=distribution,
        ),
    )


def load_football_probability_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> tuple[AnalyticalArtifact, JointScoreDistribution]:
    """Strictly verify matrix normalization and every derived market row."""
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_PROBABILITY_ARTIFACT_TYPE,
        expected_schema_version=FOOTBALL_PROBABILITY_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "canonical_event_id",
        "model_artifact_id",
        "prediction_cutoff",
        "model_family",
        "model_version",
        "competition_id",
        "calibration_method",
        "home_intensity",
        "away_intensity",
        "rho",
        "score_grid_maximum",
        "residual_tail_mass",
        "tail_tolerance",
        "fallback_used",
        "joint_score_probabilities",
        "markets",
        "price_semantics",
        "capability_state",
    }:
        raise ArtifactError("football probability artifact fields are not exact")
    if payload["capability_state"] != "fair-odds-only":
        raise ArtifactError("football probability artifact capability state is unsafe")
    semantics = payload["price_semantics"]
    if not isinstance(semantics, dict) or semantics != {
        "probability": "model-probability",
        "fair_decimal_odds": "model-estimate-not-offered-price",
        "offered_decimal_odds": None,
        "expected_value": None,
    }:
        raise ArtifactError("football probability artifact price semantics are invalid")
    maximum = _integer(payload["score_grid_maximum"], "score_grid_maximum")
    if not 1 <= maximum <= 64:
        raise ArtifactError("football probability score grid is outside hard bounds")
    rows = payload["joint_score_probabilities"]
    expected_count = (maximum + 1) ** 2
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise ArtifactError("football probability score rows have invalid cardinality")
    matrix = [[0.0] * (maximum + 1) for _ in range(maximum + 1)]
    seen: set[tuple[int, int]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "home_goals",
            "away_goals",
            "probability",
        }:
            raise ArtifactError("football probability score row fields are not exact")
        home = _integer(row["home_goals"], "home_goals")
        away = _integer(row["away_goals"], "away_goals")
        if home > maximum or away > maximum or (home, away) in seen:
            raise ArtifactError("football probability score identity is invalid")
        seen.add((home, away))
        matrix[home][away] = _probability(row["probability"], "probability")
    try:
        cutoff = date.fromisoformat(_string(payload["prediction_cutoff"], "prediction_cutoff"))
        distribution = JointScoreDistribution(
            probabilities=tuple(tuple(row) for row in matrix),
            home_intensity=_finite(payload["home_intensity"], "home_intensity"),
            away_intensity=_finite(payload["away_intensity"], "away_intensity"),
            rho=_finite(payload["rho"], "rho"),
            score_grid_maximum=maximum,
            residual_tail_mass=_probability(
                payload["residual_tail_mass"],
                "residual_tail_mass",
            ),
            tail_tolerance=_finite(payload["tail_tolerance"], "tail_tolerance"),
            model_family=_string(payload["model_family"], "model_family"),
            model_version=_string(payload["model_version"], "model_version"),
            competition_id=_string(payload["competition_id"], "competition_id"),
            prediction_cutoff=cutoff,
            fallback_used=_boolean(payload["fallback_used"], "fallback_used"),
            calibration_method=_string(
                payload["calibration_method"],
                "calibration_method",
            ),
        )
    except (ValueError, ModelError) as exc:
        raise ArtifactError(f"football probability surface is invalid: {exc}") from exc
    expected_markets = probability_surface_payload(
        canonical_event_id=_string(payload["canonical_event_id"], "canonical_event_id"),
        model_artifact_id=_string(payload["model_artifact_id"], "model_artifact_id"),
        distribution=distribution,
    )["markets"]
    actual_markets = payload["markets"]
    if actual_markets != expected_markets:
        raise ArtifactError("football derived market probabilities or fair odds were tampered")
    return artifact, distribution


def _string(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise ArtifactError(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ArtifactError(f"{field} must be a non-negative integer")
    return value


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ArtifactError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ArtifactError(f"{field} must be finite")
    return number


def _probability(value: object, field: str) -> float:
    number = _finite(value, field)
    if not 0.0 <= number <= 1.0:
        raise ArtifactError(f"{field} must lie in [0, 1]")
    return number


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ArtifactError(f"{field} must be a boolean")
    return value

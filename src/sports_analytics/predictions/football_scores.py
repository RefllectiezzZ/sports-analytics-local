"""Strict persisted coherent football probability and fair-odds artifacts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.markets.football_score_markets import (
    FootballMarketProbability,
    derive_full_time_markets,
)
from sports_analytics.models.football_scores import JointScoreDistribution

FOOTBALL_PROBABILITY_ARTIFACT_TYPE: Final[str] = "football-probability-surface"
FOOTBALL_PROBABILITY_ARTIFACT_SCHEMA: Final[str] = "football-probability-surface-v1"
FOOTBALL_PRODUCTION_PROBABILITY_ARTIFACT_SCHEMA: Final[str] = "football-probability-surface-v2"
PROSPECTIVE_OPERATOR_PROVENANCE: Final[str] = "prospective-operator"


@dataclass(frozen=True, slots=True)
class FootballProductionPredictionLineage:
    canonical_event_id: str
    competition_id: str
    model_artifact_id: str
    model_checksum_sha256: str
    active_champion_role_revision: int
    active_champion_transition_id: str | None
    predicted_at_utc: datetime
    decision_as_of_utc: datetime
    event_start_utc: datetime
    prediction_provenance: str
    upcoming_event_artifact_id: str
    upcoming_event_checksum_sha256: str
    participant_registry_artifact_id: str
    participant_registry_checksum_sha256: str


def probability_surface_payload(
    *,
    canonical_event_id: str,
    model_artifact_id: str,
    distribution: JointScoreDistribution,
    markets: tuple[FootballMarketProbability, ...] | None = None,
    participant_identity: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Serialize one bounded coherent score surface and all derived fair odds."""
    derived = markets or derive_full_time_markets(distribution)
    payload: dict[str, JsonValue] = {
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
    if participant_identity is not None:
        payload["participant_identity"] = participant_identity
    return payload


def write_football_probability_artifact(
    *,
    root: Path,
    relative_directory: str,
    canonical_event_id: str,
    model_artifact_id: str,
    distribution: JointScoreDistribution,
    participant_identity: dict[str, JsonValue] | None = None,
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
            participant_identity=participant_identity,
        ),
    )


def write_production_football_probability_artifact(
    *,
    root: Path,
    relative_directory: str,
    canonical_event_id: str,
    model_artifact_id: str,
    model_checksum_sha256: str,
    active_champion_role_revision: int,
    active_champion_transition_id: str | None,
    predicted_at_utc: datetime,
    decision_as_of_utc: datetime,
    event_start_utc: datetime,
    upcoming_event_artifact_id: str,
    upcoming_event_checksum_sha256: str,
    participant_registry_artifact_id: str,
    participant_registry_checksum_sha256: str,
    distribution: JointScoreDistribution,
    participant_identity: dict[str, JsonValue],
) -> AnalyticalArtifact:
    """Publish one production-only surface with exact prospective lineage."""
    payload = probability_surface_payload(
        canonical_event_id=canonical_event_id,
        model_artifact_id=model_artifact_id,
        distribution=distribution,
        participant_identity=participant_identity,
    )
    payload.update(
        {
            "model_checksum_sha256": model_checksum_sha256,
            "active_champion_role_revision": active_champion_role_revision,
            "active_champion_transition_id": active_champion_transition_id,
            "predicted_at_utc": format_utc_timestamp(predicted_at_utc),
            "decision_as_of_utc": format_utc_timestamp(decision_as_of_utc),
            "event_start_utc": format_utc_timestamp(event_start_utc),
            "prediction_provenance": PROSPECTIVE_OPERATOR_PROVENANCE,
            "upcoming_event_artifact_id": upcoming_event_artifact_id,
            "upcoming_event_checksum_sha256": upcoming_event_checksum_sha256,
            "participant_registry_artifact_id": participant_registry_artifact_id,
            "participant_registry_checksum_sha256": participant_registry_checksum_sha256,
        }
    )
    _verify_probability_semantics(payload, distribution=distribution)
    _verify_participant_identity(payload["participant_identity"])
    _production_lineage(payload, distribution=distribution)
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_PROBABILITY_ARTIFACT_TYPE,
        schema_version=FOOTBALL_PRODUCTION_PROBABILITY_ARTIFACT_SCHEMA,
        payload=payload,
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
    required_fields = {
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
    }
    if not isinstance(payload, dict) or set(payload) not in {
        frozenset(required_fields),
        frozenset(required_fields | {"participant_identity"}),
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


def load_production_football_probability_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
    expected_artifact_id: str | None = None,
) -> tuple[
    AnalyticalArtifact,
    JointScoreDistribution,
    FootballProductionPredictionLineage,
]:
    """Strictly reload a production surface and verify all prospective lineage."""
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_PROBABILITY_ARTIFACT_TYPE,
        expected_schema_version=FOOTBALL_PRODUCTION_PROBABILITY_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
        expected_artifact_id=expected_artifact_id,
    )
    payload = artifact.payload
    production_fields = {
        "model_checksum_sha256",
        "active_champion_role_revision",
        "active_champion_transition_id",
        "predicted_at_utc",
        "decision_as_of_utc",
        "event_start_utc",
        "prediction_provenance",
        "upcoming_event_artifact_id",
        "upcoming_event_checksum_sha256",
        "participant_registry_artifact_id",
        "participant_registry_checksum_sha256",
    }
    required_fields = {
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
        "participant_identity",
    }
    if not isinstance(payload, dict) or set(payload) != required_fields | production_fields:
        raise ArtifactError("production football probability artifact fields are not exact")
    distribution = _distribution_from_payload(payload)
    _verify_probability_semantics(payload, distribution=distribution)
    _verify_participant_identity(payload["participant_identity"])
    return artifact, distribution, _production_lineage(payload, distribution=distribution)


def _distribution_from_payload(payload: dict[str, JsonValue]) -> JointScoreDistribution:
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
        return JointScoreDistribution(
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


def _verify_probability_semantics(
    payload: dict[str, JsonValue], *, distribution: JointScoreDistribution
) -> None:
    expected_markets = probability_surface_payload(
        canonical_event_id=_string(payload["canonical_event_id"], "canonical_event_id"),
        model_artifact_id=_string(payload["model_artifact_id"], "model_artifact_id"),
        distribution=distribution,
    )["markets"]
    if payload["markets"] != expected_markets:
        raise ArtifactError("football derived market probabilities or fair odds were tampered")


def _production_lineage(
    payload: dict[str, JsonValue], *, distribution: JointScoreDistribution
) -> FootballProductionPredictionLineage:
    try:
        model_checksum = _checksum(payload["model_checksum_sha256"], "model_checksum_sha256")
        upcoming_checksum = _checksum(
            payload["upcoming_event_checksum_sha256"],
            "upcoming_event_checksum_sha256",
        )
        registry_checksum = _checksum(
            payload["participant_registry_checksum_sha256"],
            "participant_registry_checksum_sha256",
        )
        predicted = parse_utc_timestamp(_string(payload["predicted_at_utc"], "predicted_at_utc"))
        decision = parse_utc_timestamp(_string(payload["decision_as_of_utc"], "decision_as_of_utc"))
        event_start = parse_utc_timestamp(_string(payload["event_start_utc"], "event_start_utc"))
    except ValueError as exc:
        raise ArtifactError(f"production football probability lineage is invalid: {exc}") from exc
    revision = _integer(
        payload["active_champion_role_revision"],
        "active_champion_role_revision",
    )
    transition = payload["active_champion_transition_id"]
    if transition is not None:
        transition = _string(transition, "active_champion_transition_id")
    if revision < 1:
        raise ArtifactError("active_champion_role_revision must be a positive integer")
    if payload["prediction_provenance"] != PROSPECTIVE_OPERATOR_PROVENANCE:
        raise ArtifactError("production football probability provenance is invalid")
    if predicted != decision:
        raise ArtifactError("production prediction and decision timestamps must match")
    if decision >= event_start:
        raise ArtifactError("production prediction decision must precede event start")
    if distribution.prediction_cutoff != decision.date():
        raise ArtifactError("production prediction cutoff date must match decision timestamp")
    competition_id = _string(payload["competition_id"], "competition_id")
    if competition_id != distribution.competition_id:
        raise ArtifactError("production prediction competition lineage is inconsistent")
    return FootballProductionPredictionLineage(
        canonical_event_id=_string(payload["canonical_event_id"], "canonical_event_id"),
        competition_id=competition_id,
        model_artifact_id=_string(payload["model_artifact_id"], "model_artifact_id"),
        model_checksum_sha256=model_checksum,
        active_champion_role_revision=revision,
        active_champion_transition_id=transition,
        predicted_at_utc=predicted,
        decision_as_of_utc=decision,
        event_start_utc=event_start,
        prediction_provenance=PROSPECTIVE_OPERATOR_PROVENANCE,
        upcoming_event_artifact_id=_string(
            payload["upcoming_event_artifact_id"],
            "upcoming_event_artifact_id",
        ),
        upcoming_event_checksum_sha256=upcoming_checksum,
        participant_registry_artifact_id=_string(
            payload["participant_registry_artifact_id"],
            "participant_registry_artifact_id",
        ),
        participant_registry_checksum_sha256=registry_checksum,
    )


def _verify_participant_identity(value: JsonValue) -> None:
    fields = {
        "home_participant_identity_state",
        "away_participant_identity_state",
        "home_model_team_state",
        "away_model_team_state",
        "unseen_team_fallback_used",
        "unseen_participant_ids",
        "fallback_policy",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ArtifactError("production participant identity fields are not exact")
    participant_states = {
        "registered-model-seen",
        "registered-model-unseen",
    }
    model_states = {"model-seen", "competition-average-zero-effect"}
    if (
        value["home_participant_identity_state"] not in participant_states
        or value["away_participant_identity_state"] not in participant_states
        or value["home_model_team_state"] not in model_states
        or value["away_model_team_state"] not in model_states
    ):
        raise ArtifactError("production participant identity state is invalid")
    fallback = _boolean(value["unseen_team_fallback_used"], "unseen_team_fallback_used")
    unseen = value["unseen_participant_ids"]
    if not isinstance(unseen, list):
        raise ArtifactError("production unseen participant identities are invalid")
    unseen_ids = [_string(item, "unseen_participant_ids[]") for item in unseen]
    if len(unseen_ids) != len(set(unseen_ids)) or unseen_ids != sorted(unseen_ids):
        raise ArtifactError("production unseen participant identities are invalid")
    policy = value["fallback_policy"]
    if fallback != bool(unseen_ids) or policy != (
        "competition-average-zero-effect" if fallback else None
    ):
        raise ArtifactError("production participant fallback lineage is inconsistent")


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


def _checksum(value: object, field: str) -> str:
    text = _string(value, field)
    try:
        validate_sha256_checksum(text)
    except Exception as exc:
        raise ArtifactError(f"{field} must be a SHA-256 checksum") from exc
    return text

"""Exact active score-model champion resolution for production inference."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import GovernanceError
from sports_analytics.governance.contracts import ModelLifecycleStatus, ModelRole
from sports_analytics.governance.repository import ModelGovernanceRepository
from sports_analytics.models.football_scores import (
    FOOTBALL_SCORE_ARTIFACT_SCHEMA,
    FOOTBALL_SCORE_ARTIFACT_TYPE,
    FOOTBALL_SCORE_MODEL_VERSION,
    FootballScoreModel,
    load_score_model_artifact,
)

FOOTBALL_PRODUCT_MODEL_PURPOSE: Final[str] = "football-fair-odds"
FOOTBALL_PROBABILITY_GENERATOR_SCOPE: Final[str] = "football-score-surface-full-match"
FOOTBALL_PRODUCTION_EVALUATION_MODE: Final[str] = "prospective-operator"
FOOTBALL_SCORE_CALIBRATION_TYPE: Final[str] = "football-score-calibration"
FOOTBALL_SCORE_CALIBRATION_SCHEMA: Final[str] = "football-score-calibration-v1"


@dataclass(frozen=True, slots=True)
class ResolvedScoreChampion:
    model: FootballScoreModel
    model_artifact_id: str
    model_checksum_sha256: str
    model_relative_path: str
    model_family: str
    training_lineage: str
    calibration_lineage: str
    calibration_checksum_sha256: str
    calibration_temperature: float
    active_role_revision: int
    active_transition_id: str | None


def write_score_calibration_artifact(
    *,
    root: Path,
    relative_directory: str,
    model_artifact_id: str,
    training_lineage: str,
    temperature: float,
) -> AnalyticalArtifact:
    """Publish explicit score calibration for later strict production reload."""
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(float(temperature))
        or float(temperature) <= 0.0
    ):
        raise GovernanceError("score calibration temperature is invalid")
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=FOOTBALL_SCORE_CALIBRATION_TYPE,
        schema_version=FOOTBALL_SCORE_CALIBRATION_SCHEMA,
        payload={
            "method": "global-temperature",
            "temperature": float(temperature),
            "model_artifact_id": _required_text(model_artifact_id, "model artifact"),
            "training_lineage": _required_text(training_lineage, "training lineage"),
        },
    )


def load_score_calibration_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_artifact_id: str | None = None,
    expected_checksum: str | None = None,
    expected_model_artifact_id: str,
    expected_training_lineage: str,
) -> tuple[AnalyticalArtifact, float]:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=FOOTBALL_SCORE_CALIBRATION_TYPE,
        expected_schema_version=FOOTBALL_SCORE_CALIBRATION_SCHEMA,
        expected_artifact_id=expected_artifact_id,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "method",
        "temperature",
        "model_artifact_id",
        "training_lineage",
    }:
        raise GovernanceError("score calibration payload fields are not exact")
    if payload["method"] != "global-temperature":
        raise GovernanceError("score calibration method is incompatible")
    if payload["model_artifact_id"] != expected_model_artifact_id:
        raise GovernanceError("score calibration model lineage mismatch")
    if payload["training_lineage"] != expected_training_lineage:
        raise GovernanceError("score calibration training lineage mismatch")
    temperature = payload["temperature"]
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(float(temperature))
        or float(temperature) <= 0.0
    ):
        raise GovernanceError("score calibration temperature is invalid")
    return artifact, float(temperature)


def resolve_active_score_champion(
    *,
    connection: sqlite3.Connection,
    model_root: Path,
    competition_id: str,
    market_key: str,
) -> ResolvedScoreChampion | None:
    """Resolve exactly one compatible champion; never scan files or fit a model."""
    candidates = tuple(
        entry
        for entry in ModelGovernanceRepository(connection).list_models()
        if entry.role is ModelRole.CHAMPION
        and entry.lifecycle_status is ModelLifecycleStatus.PROMOTED
        and entry.sport_code == "football"
        and entry.market_key == market_key
    )
    # Provenance is a governance boundary.  Validate every potentially relevant
    # row before selection: malformed rows must not be silently hidden merely
    # because another competition happens to have a usable champion.
    active = tuple(
        entry
        for entry in candidates
        if _matches_requested_scope(entry, competition_id=competition_id)
    )
    if len(active) > 1:
        raise GovernanceError("multiple compatible active champions")
    if not active:
        return None
    entry = active[0]
    provenance = entry.provenance
    if not isinstance(provenance, dict):  # guarded above; keeps the type narrow.
        raise GovernanceError("active champion provenance is not an object")
    if entry.model_specification_version != FOOTBALL_SCORE_MODEL_VERSION:
        raise GovernanceError("active champion model specification is incompatible")
    calibration = provenance["calibration"]
    if not isinstance(calibration, dict) or set(calibration) != {
        "method",
        "relative_directory",
        "lineage_artifact_id",
        "lineage_checksum_sha256",
    }:
        raise GovernanceError("active champion calibration provenance is invalid")
    if calibration["method"] != "global-temperature":
        raise GovernanceError("active champion calibration method is incompatible")
    training_lineage = _required_text(provenance["training_lineage"], "training_lineage")
    artifact, model = load_score_model_artifact(
        root=model_root,
        relative_directory=entry.model_relative_path,
        expected_checksum=entry.model_checksum_sha256,
        expected_artifact_id=entry.model_artifact_id,
    )
    if model.competition_id != competition_id:
        raise GovernanceError("active champion artifact competition mismatch")
    if model.model_family != provenance["model_family"]:
        raise GovernanceError("active champion model family mismatch")
    if artifact.artifact_type != provenance["artifact_type"]:
        raise GovernanceError("active champion artifact type mismatch")
    calibration_artifact, temperature = load_score_calibration_artifact(
        root=model_root,
        relative_directory=_required_text(
            calibration["relative_directory"], "calibration relative directory"
        ),
        expected_artifact_id=_required_text(
            calibration["lineage_artifact_id"], "calibration lineage"
        ),
        expected_checksum=_required_text(
            calibration["lineage_checksum_sha256"], "calibration checksum"
        ),
        expected_model_artifact_id=entry.model_artifact_id,
        expected_training_lineage=training_lineage,
    )
    return ResolvedScoreChampion(
        model=model,
        model_artifact_id=entry.model_artifact_id,
        model_checksum_sha256=entry.model_checksum_sha256,
        model_relative_path=entry.model_relative_path,
        model_family=model.model_family,
        training_lineage=training_lineage,
        calibration_lineage=calibration_artifact.artifact_id,
        calibration_checksum_sha256=calibration_artifact.checksum_sha256,
        calibration_temperature=temperature,
        active_role_revision=entry.version,
        active_transition_id=_active_transition_id(
            connection,
            model_artifact_id=entry.model_artifact_id,
        ),
    )


def _matches_requested_scope(entry: object, *, competition_id: str) -> bool:
    """Validate candidate provenance and select only its exact product scope."""
    provenance = getattr(entry, "provenance", None)
    if not isinstance(provenance, dict):
        raise GovernanceError("active champion provenance is not an object")
    required = {
        "competition_id",
        "model_purpose",
        "probability_generator_scope",
        "evaluation_mode",
        "artifact_type",
        "artifact_schema",
        "model_family",
        "training_lineage",
        "calibration",
    }
    if set(provenance) != required:
        raise GovernanceError("active champion provenance fields are not exact")
    expected = {
        "model_purpose": FOOTBALL_PRODUCT_MODEL_PURPOSE,
        "probability_generator_scope": FOOTBALL_PROBABILITY_GENERATOR_SCOPE,
        "evaluation_mode": FOOTBALL_PRODUCTION_EVALUATION_MODE,
        "artifact_type": FOOTBALL_SCORE_ARTIFACT_TYPE,
        "artifact_schema": FOOTBALL_SCORE_ARTIFACT_SCHEMA,
    }
    if any(provenance[key] != value for key, value in expected.items()):
        return False
    return (
        type(provenance["competition_id"]) is str and provenance["competition_id"] == competition_id
    )


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value:
        raise GovernanceError(f"active champion {field} is absent")
    return value


def _active_transition_id(connection: sqlite3.Connection, *, model_artifact_id: str) -> str | None:
    try:
        row = connection.execute(
            """
            SELECT id
            FROM model_role_transitions
            WHERE new_champion_model_artifact_id = ?
              AND transition_type IN ('promotion', 'rollback')
            ORDER BY occurred_at DESC, id DESC
            LIMIT 1
            """,
            (model_artifact_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # A focused in-memory resolver fixture may contain only the registry
        # table. The registry row version remains the authoritative active-role
        # revision in that case.
        return None
    return None if row is None else str(row["id"])

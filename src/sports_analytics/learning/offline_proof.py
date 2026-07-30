"""Deterministic synthetic-contract closed-loop lifecycle proof artifact."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sports_analytics.artifact_strict import require_dict, require_str
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ModelError
from sports_analytics.data.types import JsonValue

OFFLINE_PROOF_TYPE: Final[str] = "closed-loop-offline-proof"
OFFLINE_PROOF_SCHEMA: Final[str] = "closed-loop-offline-proof-v1"
OFFLINE_PROOF_STAGES: Final[tuple[str, ...]] = (
    "historical-training-fixture",
    "model-artifact-a",
    "upcoming-event-prediction",
    "operator-current-quote",
    "proposed-single",
    "event-completion",
    "verified-result-snapshot",
    "analytical-settlement",
    "monitoring",
    "training-eligibility",
    "retraining-trigger",
    "challenger-artifact-b",
    "challenger-evaluation",
    "champion-unchanged-before-explicit-promotion",
    "explicit-manual-promotion",
    "new-prediction-references-model-b",
    "rollback-restores-model-a",
)


@dataclass(frozen=True, slots=True)
class OfflineClosedLoopProof:
    model_artifact_a: str
    prediction_artifact_a: str
    result_snapshot_id: str
    settlement_artifact_id: str
    monitoring_artifact_id: str
    training_ledger_artifact_id: str
    challenger_artifact_b: str
    tournament_artifact_id: str
    prediction_artifact_b: str
    stages: tuple[str, ...] = OFFLINE_PROOF_STAGES
    provenance: str = "synthetic-contract"
    final_champion_artifact_id: str = ""

    def __post_init__(self) -> None:
        if self.stages != OFFLINE_PROOF_STAGES or self.provenance != "synthetic-contract":
            raise ModelError("offline proof sequence or provenance is invalid")
        values = (
            self.model_artifact_a,
            self.prediction_artifact_a,
            self.result_snapshot_id,
            self.settlement_artifact_id,
            self.monitoring_artifact_id,
            self.training_ledger_artifact_id,
            self.challenger_artifact_b,
            self.tournament_artifact_id,
            self.prediction_artifact_b,
        )
        if any(not value or value != value.strip() for value in values):
            raise ModelError("offline proof artifact identities must be non-empty")
        if self.model_artifact_a == self.challenger_artifact_b:
            raise ModelError("offline proof challenger must be a new immutable artifact")
        if self.final_champion_artifact_id != self.model_artifact_a:
            raise ModelError("offline proof rollback must restore model artifact A")


def write_offline_closed_loop_proof(
    *,
    root: Path,
    relative_directory: str,
    proof: OfflineClosedLoopProof,
) -> AnalyticalArtifact:
    payload: dict[str, JsonValue] = {
        "provenance": proof.provenance,
        "stages": list(proof.stages),
        "artifacts": {
            "model_artifact_a": proof.model_artifact_a,
            "prediction_artifact_a": proof.prediction_artifact_a,
            "result_snapshot_id": proof.result_snapshot_id,
            "settlement_artifact_id": proof.settlement_artifact_id,
            "monitoring_artifact_id": proof.monitoring_artifact_id,
            "training_ledger_artifact_id": proof.training_ledger_artifact_id,
            "challenger_artifact_b": proof.challenger_artifact_b,
            "tournament_artifact_id": proof.tournament_artifact_id,
            "prediction_artifact_b": proof.prediction_artifact_b,
            "final_champion_artifact_id": proof.final_champion_artifact_id,
        },
        "champion_replacement_state": "explicit-manual-only",
        "rollback_state": "model-a-restored",
    }
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=OFFLINE_PROOF_TYPE,
        schema_version=OFFLINE_PROOF_SCHEMA,
        payload=payload,
    )


def load_offline_closed_loop_proof(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=OFFLINE_PROOF_TYPE,
        expected_schema_version=OFFLINE_PROOF_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "provenance",
            "stages",
            "artifacts",
            "champion_replacement_state",
            "rollback_state",
        }
        or payload.get("provenance") != "synthetic-contract"
        or payload.get("stages") != list(OFFLINE_PROOF_STAGES)
        or payload.get("champion_replacement_state") != "explicit-manual-only"
        or payload.get("rollback_state") != "model-a-restored"
    ):
        raise ArtifactError("offline closed-loop proof trust state is invalid")
    identities = require_dict(payload["artifacts"], field="artifacts")
    if set(identities) != {
        "model_artifact_a",
        "prediction_artifact_a",
        "result_snapshot_id",
        "settlement_artifact_id",
        "monitoring_artifact_id",
        "training_ledger_artifact_id",
        "challenger_artifact_b",
        "tournament_artifact_id",
        "prediction_artifact_b",
        "final_champion_artifact_id",
    }:
        raise ArtifactError("offline closed-loop proof artifact fields are not exact")
    try:
        OfflineClosedLoopProof(
            model_artifact_a=require_str(
                identities["model_artifact_a"],
                field="model_artifact_a",
            ),
            prediction_artifact_a=require_str(
                identities["prediction_artifact_a"],
                field="prediction_artifact_a",
            ),
            result_snapshot_id=require_str(
                identities["result_snapshot_id"],
                field="result_snapshot_id",
            ),
            settlement_artifact_id=require_str(
                identities["settlement_artifact_id"],
                field="settlement_artifact_id",
            ),
            monitoring_artifact_id=require_str(
                identities["monitoring_artifact_id"],
                field="monitoring_artifact_id",
            ),
            training_ledger_artifact_id=require_str(
                identities["training_ledger_artifact_id"],
                field="training_ledger_artifact_id",
            ),
            challenger_artifact_b=require_str(
                identities["challenger_artifact_b"],
                field="challenger_artifact_b",
            ),
            tournament_artifact_id=require_str(
                identities["tournament_artifact_id"],
                field="tournament_artifact_id",
            ),
            prediction_artifact_b=require_str(
                identities["prediction_artifact_b"],
                field="prediction_artifact_b",
            ),
            final_champion_artifact_id=require_str(
                identities["final_champion_artifact_id"],
                field="final_champion_artifact_id",
            ),
        )
    except ModelError as exc:
        raise ArtifactError(str(exc)) from exc
    return artifact

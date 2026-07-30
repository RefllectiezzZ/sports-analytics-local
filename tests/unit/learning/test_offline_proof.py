from __future__ import annotations

import pytest

from sports_analytics.core.exceptions import ModelError
from sports_analytics.learning.offline_proof import (
    OfflineClosedLoopProof,
    load_offline_closed_loop_proof,
    write_offline_closed_loop_proof,
)


def _proof(**overrides: str) -> OfflineClosedLoopProof:
    values = {
        "model_artifact_a": "model-a",
        "prediction_artifact_a": "prediction-a",
        "result_snapshot_id": "result",
        "settlement_artifact_id": "settlement",
        "monitoring_artifact_id": "monitoring",
        "training_ledger_artifact_id": "ledger",
        "challenger_artifact_b": "model-b",
        "tournament_artifact_id": "tournament",
        "prediction_artifact_b": "prediction-b",
        "final_champion_artifact_id": "model-a",
    }
    values.update(overrides)
    return OfflineClosedLoopProof(**values)


def test_offline_closed_loop_proof_is_strict_and_rollback_is_required(tmp_path) -> None:
    proof = _proof()
    artifact = write_offline_closed_loop_proof(
        root=tmp_path,
        relative_directory="proof",
        proof=proof,
    )
    loaded = load_offline_closed_loop_proof(
        root=tmp_path,
        relative_directory="proof",
        expected_checksum=artifact.checksum_sha256,
    )
    assert loaded.artifact_id == artifact.artifact_id
    with pytest.raises(ModelError, match="rollback"):
        _proof(final_champion_artifact_id="model-b")

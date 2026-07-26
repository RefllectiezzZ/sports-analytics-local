from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sports_analytics.core.exceptions import GovernanceError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.governance.contracts import (
    ModelEvaluationEvidence,
    PromotionPolicy,
    evaluate_challenger,
)
from sports_analytics.governance.repository import ModelGovernanceRepository

AS_OF = datetime(2026, 5, 1, tzinfo=UTC)
START = datetime(2026, 4, 1, tzinfo=UTC)


def _insert_models(connection: object) -> None:
    for model_id, role, status, checksum in (
        ("champion", "champion", "promoted", "a" * 64),
        ("challenger", "challenger", "eligible", "b" * 64),
    ):
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO model_registry_entries (
                model_artifact_id, model_checksum_sha256, model_relative_path,
                model_specification_version, feature_specification_version,
                sport_code, market_key, role, lifecycle_status, registered_at,
                actor, provenance_json, version
            ) VALUES (?, ?, ?, 'model-v1', 'features-v1', 'football',
                      'football:match', ?, ?, '2026-01-01T00:00:00.000000Z',
                      'test', '{}', 1)
            """,
            (model_id, checksum, f"{model_id}/model.json", role, status),
        )


def _evidence(model_id: str, loss: float, brier: float) -> ModelEvaluationEvidence:
    return ModelEvaluationEvidence(
        evidence_artifact_id=f"evidence-{model_id}",
        evidence_checksum_sha256=("c" if model_id == "champion" else "d") * 64,
        model_artifact_id=model_id,
        sport_code="football",
        market_key="football:match",
        evaluation_mode="rolling-origin",
        window_start_utc=START,
        window_end_utc=AS_OF,
        event_population_id="common-events",
        sample_size=200,
        completed_result_count=200,
        coverage=1,
        log_loss=loss,
        multiclass_brier_score=brier,
        calibration_error=0.1 if model_id == "champion" else 0.09,
    )


def _record_decision(repository: ModelGovernanceRepository) -> str:
    champion = repository.get_model("champion")
    challenger = repository.get_model("challenger")
    assert champion is not None and challenger is not None
    decision = evaluate_challenger(
        champion=champion,
        challenger=challenger,
        champion_evidence=_evidence("champion", 0.8, 0.6),
        challenger_evidence=_evidence("challenger", 0.7, 0.5),
        policy=PromotionPolicy(),
        as_of_utc=AS_OF,
    )
    repository.record_decision(decision=decision, actor="test", created_at=AS_OF)
    return decision.decision_id


def test_atomic_idempotent_promotion_and_audited_rollback(tmp_path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    with connect_database(database) as connection:
        with transaction(connection):
            _insert_models(connection)
            repository = ModelGovernanceRepository(connection)
            decision_id = _record_decision(repository)
        with transaction(connection, immediate=True):
            transition_id = repository.apply_promotion(
                decision_id=decision_id,
                actor="test",
                occurred_at=AS_OF,
            )
        assert repository.get_model("challenger").role.value == "champion"  # type: ignore[union-attr]
        with transaction(connection, immediate=True):
            assert (
                repository.apply_promotion(
                    decision_id=decision_id,
                    actor="test",
                    occurred_at=AS_OF,
                )
                == transition_id
            )
        with transaction(connection, immediate=True):
            rollback_id = repository.rollback_transition(
                transition_id=transition_id,
                actor="test",
                occurred_at=AS_OF,
            )
        assert repository.get_model("champion").role.value == "champion"  # type: ignore[union-attr]
        assert repository.list_history()[-1]["transition_id"] == rollback_id


def test_stale_promotion_decision_rejected_without_partial_role_change(tmp_path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    with connect_database(database) as connection:
        with transaction(connection):
            _insert_models(connection)
            repository = ModelGovernanceRepository(connection)
            decision_id = _record_decision(repository)
            connection.execute(
                """
                UPDATE model_registry_entries
                SET version = version + 1
                WHERE model_artifact_id = 'challenger'
                """
            )
        with pytest.raises(GovernanceError, match="stale"):
            with transaction(connection, immediate=True):
                repository.apply_promotion(
                    decision_id=decision_id,
                    actor="test",
                    occurred_at=AS_OF,
                )
        assert repository.get_model("champion").role.value == "champion"  # type: ignore[union-attr]
        assert repository.get_model("challenger").role.value == "challenger"  # type: ignore[union-attr]

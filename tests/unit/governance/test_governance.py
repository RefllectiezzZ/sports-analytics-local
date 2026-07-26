from __future__ import annotations

from datetime import UTC, datetime

from sports_analytics.governance.contracts import (
    GovernanceDecisionKind,
    ModelEvaluationEvidence,
    ModelLifecycleStatus,
    ModelRegistryEntry,
    ModelRole,
    PromotionPolicy,
    evaluate_challenger,
)

AS_OF = datetime(2026, 4, 2, tzinfo=UTC)
START = datetime(2026, 3, 1, tzinfo=UTC)


def _entry(model_id: str, role: ModelRole, *, market: str = "football:match") -> ModelRegistryEntry:
    return ModelRegistryEntry(
        model_artifact_id=model_id,
        model_checksum_sha256=("a" if role is ModelRole.CHAMPION else "b") * 64,
        model_relative_path=f"{model_id}/model.json",
        model_specification_version="model-v1",
        feature_specification_version="features-v1",
        sport_code="football",
        market_key=market,
        registered_at=START,
        role=role,
        lifecycle_status=ModelLifecycleStatus.PROMOTED
        if role is ModelRole.CHAMPION
        else ModelLifecycleStatus.ELIGIBLE,
        actor="test",
        provenance={"fixture": True},
    )


def _evidence(
    model_id: str,
    *,
    log_loss: float,
    brier: float,
    calibration: float = 0.1,
    sample: int = 200,
    coverage: float = 1.0,
    market: str = "football:match",
) -> ModelEvaluationEvidence:
    return ModelEvaluationEvidence(
        evidence_artifact_id=f"evidence-{model_id}",
        evidence_checksum_sha256=("c" if model_id == "champion" else "d") * 64,
        model_artifact_id=model_id,
        sport_code="football",
        market_key=market,
        evaluation_mode="rolling-origin",
        window_start_utc=START,
        window_end_utc=AS_OF,
        event_population_id="events-common",
        sample_size=sample,
        completed_result_count=sample,
        coverage=coverage,
        log_loss=log_loss,
        multiclass_brier_score=brier,
        calibration_error=calibration,
    )


def _decision(
    champion: ModelEvaluationEvidence,
    challenger: ModelEvaluationEvidence,
) -> GovernanceDecisionKind:
    return evaluate_challenger(
        champion=_entry("champion", ModelRole.CHAMPION),
        challenger=_entry("challenger", ModelRole.CHALLENGER),
        champion_evidence=champion,
        challenger_evidence=challenger,
        policy=PromotionPolicy(
            minimum_sample_size=100,
            minimum_log_loss_improvement=0.01,
            minimum_brier_improvement=0.01,
            minimum_calibration_improvement=0.0,
        ),
        as_of_utc=AS_OF,
    ).decision


def test_insufficient_sample_holds() -> None:
    assert (
        _decision(
            _evidence("champion", log_loss=0.8, brier=0.6, sample=50),
            _evidence("challenger", log_loss=0.7, brier=0.5, sample=50),
        )
        is GovernanceDecisionKind.HOLD
    )


def test_exact_tie_and_materially_worse_retain() -> None:
    assert (
        _decision(
            _evidence("champion", log_loss=0.8, brier=0.6),
            _evidence("challenger", log_loss=0.8, brier=0.6),
        )
        is GovernanceDecisionKind.RETAIN
    )
    assert (
        _decision(
            _evidence("champion", log_loss=0.8, brier=0.6),
            _evidence("challenger", log_loss=0.9, brier=0.7),
        )
        is GovernanceDecisionKind.RETAIN
    )


def test_qualifying_challenger_recommends_promotion() -> None:
    assert (
        _decision(
            _evidence("champion", log_loss=0.8, brier=0.6),
            _evidence("challenger", log_loss=0.7, brier=0.5),
        )
        is GovernanceDecisionKind.PROMOTE
    )


def test_incompatible_scope_rejects() -> None:
    decision = evaluate_challenger(
        champion=_entry("champion", ModelRole.CHAMPION),
        challenger=_entry("challenger", ModelRole.CHALLENGER, market="football:other"),
        champion_evidence=_evidence("champion", log_loss=0.8, brier=0.6),
        challenger_evidence=_evidence(
            "challenger",
            log_loss=0.7,
            brier=0.5,
            market="football:other",
        ),
        policy=PromotionPolicy(),
        as_of_utc=AS_OF,
    )
    assert decision.decision is GovernanceDecisionKind.REJECT

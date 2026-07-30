from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.artifacts import write_analytical_artifact
from sports_analytics.core.exceptions import ArtifactError, EvaluationError
from sports_analytics.economics.football_evidence import (
    FOOTBALL_ECONOMIC_POLICY_VERSION,
    EconomicArtifactReference,
    FootballEconomicEligibilityPolicy,
    evaluate_football_economic_evidence,
    load_football_economic_evidence,
    write_football_economic_evidence,
)

NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)


def _policy() -> FootballEconomicEligibilityPolicy:
    return FootballEconomicEligibilityPolicy(
        minimum_prospective_prediction_count=2,
        minimum_timestamped_quote_count=2,
        minimum_completed_settlement_count=2,
        minimum_settlement_coverage=1.0,
        maximum_calibration_error=0.1,
        maximum_log_loss=1.0,
        maximum_brier_score=0.8,
        maximum_rps=0.5,
        maximum_market_baseline_degradation=0.01,
        minimum_realised_roi=0.01,
        maximum_drawdown=0.2,
        maximum_evidence_age=timedelta(days=2),
    )


def _payload(tmp_path, *, policy: FootballEconomicEligibilityPolicy, **overrides):
    upstream = write_analytical_artifact(
        root=tmp_path,
        relative_directory="upstream",
        artifact_type="test-prospective-input",
        schema_version="test-prospective-input-v1",
        payload={"state": "verified"},
    )
    reference = EconomicArtifactReference(
        upstream.relative_directory,
        upstream.artifact_id,
        upstream.checksum_sha256,
        upstream.artifact_type,
        upstream.schema_version,
    )
    payload = {
        "sport_code": "football",
        "competition_id": "prt-primeira-liga",
        "market_key": "football-1x2-full-time",
        "model_artifact_id": "model-verified",
        "model_checksum_sha256": "1" * 64,
        "champion_role_revision": 4,
        "champion_transition_id": "transition-verified",
        "evaluation_mode": "prospective-operator",
        "evidence_window_start_utc": "2026-08-08T12:00:00.000000Z",
        "evidence_window_end_utc": "2026-08-10T11:00:00.000000Z",
        "evaluated_at_utc": "2026-08-10T12:00:00.000000Z",
        "prediction_population_id": "prospective-predictions-v1",
        "quote_population_id": "timestamped-quotes-v1",
        "result_population_id": "verified-results-v1",
        "settlement_population_id": "verified-settlements-v1",
        "monitoring_population_id": "monitoring-v1",
        "policy_id": policy.policy_id,
        "policy_version": FOOTBALL_ECONOMIC_POLICY_VERSION,
        "policy_configuration_id": policy.configuration_id,
        "prospective_prediction_count": 2,
        "timestamped_quote_count": 2,
        "completed_settlement_count": 2,
        "settlement_coverage": 1.0,
        "log_loss": 0.5,
        "multiclass_brier_score": 0.4,
        "ranked_probability_score": 0.2,
        "calibration_error": 0.02,
        "market_baseline_log_loss": 0.51,
        "market_baseline_brier_score": 0.41,
        "market_baseline_rps": 0.21,
        "realised_turnover": 10.0,
        "realised_profit_loss": 1.0,
        "realised_roi": 0.1,
        "maximum_drawdown": 0.05,
        "unresolved_settlement_count": 0,
        "stale_or_invalid_quote_count": 0,
        "source_classification": "verified-prospective-operator-artifacts",
        "evidence_derivation_version": "football-economic-derivation-v1",
        "upstream_artifacts": [reference.to_json()],
    }
    payload.update(overrides)
    return payload


def _decision(evidence, policy):
    return evaluate_football_economic_evidence(
        evidence=evidence,
        policy=policy,
        model_artifact_id="model-verified",
        model_checksum_sha256="1" * 64,
        competition_id="prt-primeira-liga",
        market_key="football-1x2-full-time",
        champion_role_revision=4,
        champion_transition_id="transition-verified",
        evaluated_at_utc=NOW,
    )


def test_prospective_evidence_is_strictly_reloaded_and_can_pass(tmp_path) -> None:
    policy = _policy()
    artifact = write_football_economic_evidence(
        root=tmp_path,
        relative_directory="economic",
        payload=_payload(tmp_path, policy=policy),
    )
    evidence = load_football_economic_evidence(
        root=tmp_path,
        relative_directory="economic",
        expected_artifact_id=artifact.artifact_id,
        expected_checksum=artifact.checksum_sha256,
    )
    decision = _decision(evidence, policy)
    assert decision.opportunity_analysis_eligible is True
    assert decision.bet_proposal_eligible is True
    assert decision.promotion_eligible is True
    assert not decision.hold_reasons


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("prospective_prediction_count", 1, "insufficient-prospective-sample"),
        ("timestamped_quote_count", 1, "insufficient-timestamped-quote-sample"),
        ("completed_settlement_count", 1, "insufficient-completed-settlement-sample"),
        ("calibration_error", 0.2, "calibration-threshold-failed"),
        ("realised_roi", -0.1, "economic-return-threshold-failed"),
        ("maximum_drawdown", 0.3, "drawdown-threshold-failed"),
    ],
)
def test_policy_rejections_are_typed(tmp_path, field, value, reason) -> None:
    policy = _policy()
    payload = _payload(tmp_path, policy=policy)
    payload[field] = value
    if field in {"prospective_prediction_count", "timestamped_quote_count"}:
        payload["completed_settlement_count"] = 1
        payload["settlement_coverage"] = 1.0
    if field == "completed_settlement_count":
        payload["settlement_coverage"] = 0.5
    artifact = write_football_economic_evidence(
        root=tmp_path, relative_directory="economic", payload=payload
    )
    decision = _decision(
        load_football_economic_evidence(
            root=tmp_path,
            relative_directory="economic",
            expected_checksum=artifact.checksum_sha256,
        ),
        policy,
    )
    assert decision.bet_proposal_eligible is False
    assert reason in decision.hold_reasons


def test_historical_evidence_never_authorizes_proposals(tmp_path) -> None:
    policy = _policy()
    payload = _payload(tmp_path, policy=policy, evaluation_mode="historical-closing-benchmark")
    artifact = write_football_economic_evidence(
        root=tmp_path, relative_directory="economic", payload=payload
    )
    decision = _decision(
        load_football_economic_evidence(
            root=tmp_path, relative_directory="economic", expected_checksum=artifact.checksum_sha256
        ),
        policy,
    )
    assert decision.bet_proposal_eligible is False
    assert "historical-closing-only-evidence" in decision.hold_reasons


def test_semantic_and_upstream_tampering_fail_closed(tmp_path) -> None:
    policy = _policy()
    artifact = write_football_economic_evidence(
        root=tmp_path, relative_directory="economic", payload=_payload(tmp_path, policy=policy)
    )
    manifest = tmp_path / "economic" / "manifest.json"
    manifest.write_text(manifest.read_text().replace("model-verified", "forged-model"))
    with pytest.raises(ArtifactError, match="checksum"):
        load_football_economic_evidence(root=tmp_path, relative_directory="economic")
    assert artifact.artifact_id


def test_invalid_coverage_and_unsafe_reference_are_rejected(tmp_path) -> None:
    policy = _policy()
    payload = _payload(tmp_path, policy=policy, settlement_coverage=0.5)
    with pytest.raises(EvaluationError, match="coverage"):
        write_football_economic_evidence(root=tmp_path, relative_directory="bad", payload=payload)
    payload = _payload(tmp_path / "separate", policy=policy)
    payload["upstream_artifacts"][0]["relative_directory"] = "../escape"
    with pytest.raises(EvaluationError, match="unsafe"):
        write_football_economic_evidence(
            root=tmp_path, relative_directory="bad-ref", payload=payload
        )

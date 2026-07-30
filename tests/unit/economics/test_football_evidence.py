from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.artifacts import (
    build_analytical_artifact_document,
    load_analytical_artifact,
)
from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorEventReference,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
    validate_operator_quotes,
    write_operator_quote_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, EvaluationError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.economics.football_evidence import (
    MONITORING_ROLE,
    PREDICTIONS_ROLE,
    QUOTES_ROLE,
    RESULTS_ROLE,
    SETTLEMENTS_ROLE,
    ChampionEconomicIdentity,
    EconomicArtifactReference,
    FootballEconomicEligibilityPolicy,
    FootballEconomicEvidence,
    derive_football_economic_evidence,
    evaluate_football_economic_evidence,
    load_football_economic_evidence,
    parse_economic_evaluation_request,
)
from sports_analytics.models.football_scores import JointScoreDistribution
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.monitoring.artifacts import publish_monitoring_report
from sports_analytics.monitoring.contracts import (
    DEFAULT_MONITORING_POLICY,
    EvidenceReference,
    MonitoringInputs,
    PerformanceObservation,
    evaluate_monitoring,
)
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.predictions.football_scores import write_football_probability_artifact
from sports_analytics.results.contracts import (
    EventResultStatus,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import (
    RESULT_SNAPSHOT_ARTIFACT_TYPE,
    RESULT_SNAPSHOT_SCHEMA_VERSION,
    publish_result_snapshot,
)
from sports_analytics.settlement.contracts import SETTLEMENT_POLICY_V1, settle_single
from sports_analytics.settlement.service import SettlementReport, publish_settlement_report
from sports_analytics.sports.football.markets import match_result_1x2_selection

EVENT_ID = "event-verified-1"
MODEL_ID = "model-verified"
MODEL_CHECKSUM = "1" * 64
QUOTE_AT = datetime(2026, 8, 8, 12, tzinfo=UTC)
RESULT_AT = datetime(2026, 8, 9, 17, tzinfo=UTC)
SETTLED_AT = datetime(2026, 8, 9, 18, tzinfo=UTC)
NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
CHAMPION = ChampionEconomicIdentity(MODEL_ID, MODEL_CHECKSUM, 4, "transition-verified")


def _reference(role, artifact):
    return EconomicArtifactReference(
        role,
        artifact.relative_directory,
        artifact.artifact_id,
        artifact.checksum_sha256,
        artifact.artifact_type,
        artifact.schema_version,
    )


def _policy(**changes) -> FootballEconomicEligibilityPolicy:
    policy = FootballEconomicEligibilityPolicy(
        minimum_prospective_prediction_count=1,
        minimum_timestamped_quote_count=3,
        minimum_completed_settlement_count=1,
        minimum_settlement_coverage=1.0,
        maximum_calibration_error=1.0,
        maximum_log_loss=2.0,
        maximum_brier_score=1.0,
        maximum_rps=1.0,
        maximum_market_baseline_degradation=0.2,
        minimum_realised_roi=0.01,
        maximum_drawdown=1.0,
        maximum_evidence_age=timedelta(days=2),
    )
    return replace(policy, **changes)


def _typed_sources(tmp_path):
    raw_probabilities = tuple(
        tuple(
            math.exp(-2.0)
            * 2.0**home
            / math.factorial(home)
            * math.exp(-0.7)
            * 0.7**away
            / math.factorial(away)
            for away in range(8)
        )
        for home in range(8)
    )
    total_probability = math.fsum(value for row in raw_probabilities for value in row)
    distribution = JointScoreDistribution(
        probabilities=tuple(
            tuple(value / total_probability for value in row) for row in raw_probabilities
        ),
        home_intensity=2.0,
        away_intensity=0.7,
        rho=0.0,
        score_grid_maximum=7,
        residual_tail_mass=0.0,
        tail_tolerance=0.01,
        model_family="independent-poisson",
        model_version="football-score-model-v1",
        competition_id="prt-primeira-liga",
        prediction_cutoff=date(2026, 8, 8),
        fallback_used=False,
    )
    prediction = write_football_probability_artifact(
        root=tmp_path,
        relative_directory="upstream/prediction",
        canonical_event_id=EVENT_ID,
        model_artifact_id=MODEL_ID,
        distribution=distribution,
    )
    quote_inputs = tuple(
        OperatorQuoteInput(
            provider_id="operator-book",
            provider_display_name="Operator Book",
            sport_code="football",
            canonical_event_id=EVENT_ID,
            market_family="match-result",
            outcome_key=outcome,
            line_value=None,
            market_period="full-match",
            participant_scope="event",
            canonical_participant_id=None,
            overtime_scope=REGULATION_SCOPE,
            rules_scope=FOOTBALL_RULES_SCOPE,
            offered_decimal_odds=Decimal(odds),
            observed_at_utc=QUOTE_AT,
            valid_until_utc=QUOTE_AT + timedelta(hours=1),
            source_kind=OperatorQuoteSourceKind.MANUAL,
        )
        for outcome, odds in (("home", "1.50"), ("draw", "5.00"), ("away", "10.00"))
    )
    catalogue = validate_operator_quotes(
        quote_inputs,
        registered_provider_ids=frozenset({"operator-book"}),
        events=(
            OperatorEventReference(
                EVENT_ID,
                "football",
                datetime(2026, 8, 9, 15, tzinfo=UTC),
            ),
        ),
        evaluated_at_utc=QUOTE_AT,
    )
    quotes = write_operator_quote_artifact(
        root=tmp_path,
        relative_directory="upstream/quotes",
        catalogue=catalogue,
    )
    result = build_football_full_match_1x2_result(
        canonical_event_id=EVENT_ID,
        scheduled_start_utc=datetime(2026, 8, 9, 15, tzinfo=UTC),
        event_status=EventResultStatus.COMPLETED,
        source_name="verified-results",
        source_event_id="source-event-1",
        source_observed_at_utc=RESULT_AT,
        source_checksum_sha256="2" * 64,
        result_provenance="verified-result-feed",
        home_canonical_participant_id="home-team",
        away_canonical_participant_id="away-team",
        full_time_home_score=2,
        full_time_away_score=0,
        result_timestamp_utc=RESULT_AT,
    )
    result_snapshot = publish_result_snapshot(
        root=tmp_path,
        relative_directory="upstream/result",
        result=result,
    )
    result_artifact = load_analytical_artifact(
        root=tmp_path,
        relative_directory="upstream/result",
        expected_artifact_type=RESULT_SNAPSHOT_ARTIFACT_TYPE,
        expected_schema_version=RESULT_SNAPSHOT_SCHEMA_VERSION,
    )
    settlement = settle_single(
        source_artifact_id="analysis-artifact",
        source_artifact_checksum_sha256="3" * 64,
        opportunity_id="opportunity-1",
        canonical_event_id=EVENT_ID,
        selection=CanonicalSelectionIdentity.from_selection(match_result_1x2_selection("home")),
        decimal_odds=Decimal("1.50"),
        result_snapshot=result_snapshot,
        as_of_utc=SETTLED_AT,
    )
    run_id = content_addressed_id(
        identity_type="analytical-settlement-run-v1",
        payload={
            "source_artifact_id": "analysis-artifact",
            "source_artifact_checksum_sha256": "3" * 64,
            "policy_id": SETTLEMENT_POLICY_V1.policy_id,
            "policy_version": SETTLEMENT_POLICY_V1.policy_version,
            "as_of_utc": "2026-08-09T18:00:00.000000Z",
            "settlement_ids": [settlement.settlement_id],
        },
    )
    settlement_report = publish_settlement_report(
        root=tmp_path,
        relative_directory="upstream/settlements",
        report=SettlementReport(
            run_id,
            "analysis-artifact",
            "3" * 64,
            SETTLEMENT_POLICY_V1.policy_id,
            SETTLEMENT_POLICY_V1.policy_version,
            SETTLED_AT,
            (settlement,),
        ),
    )
    assert settlement_report.artifact is not None
    monitoring = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(
                EvidenceReference(
                    "settlements",
                    settlement_report.artifact.artifact_id,
                    settlement_report.artifact.checksum_sha256,
                ),
            ),
            prediction_count=1,
            probability_complete_count=1,
            performance=(
                PerformanceObservation(
                    "observation-1",
                    (0.75, 0.20, 0.05),
                    0,
                    True,
                    True,
                    0.5,
                ),
            ),
        ),
        policy=DEFAULT_MONITORING_POLICY,
        window_start_utc=datetime(2026, 8, 8, tzinfo=UTC),
        window_end_utc=datetime(2026, 8, 9, 19, tzinfo=UTC),
        as_of_utc=NOW,
    )
    monitoring_artifact = publish_monitoring_report(
        root=tmp_path,
        relative_directory="upstream/monitoring",
        report=monitoring,
    )
    return tuple(
        sorted(
            (
                _reference(PREDICTIONS_ROLE, prediction),
                _reference(QUOTES_ROLE, quotes),
                _reference(RESULTS_ROLE, result_artifact),
                _reference(SETTLEMENTS_ROLE, settlement_report.artifact),
                _reference(MONITORING_ROLE, monitoring_artifact),
            )
        )
    )


def _derive(tmp_path, *, policy=None):
    selected_policy = policy or _policy()
    references = _typed_sources(tmp_path)
    artifact = derive_football_economic_evidence(
        root=tmp_path,
        relative_directory="economic",
        references=references,
        champion=CHAMPION,
        policy=selected_policy,
    )
    evidence = load_football_economic_evidence(
        root=tmp_path,
        relative_directory="economic",
        expected_artifact_id=artifact.artifact_id,
        expected_checksum=artifact.checksum_sha256,
    )
    return artifact, evidence, selected_policy


def _decision(evidence, policy):
    return evaluate_football_economic_evidence(
        evidence=evidence,
        policy=policy,
        model_artifact_id=MODEL_ID,
        model_checksum_sha256=MODEL_CHECKSUM,
        competition_id="prt-primeira-liga",
        market_key="football.score.full-match",
        champion_role_revision=4,
        champion_transition_id="transition-verified",
        evaluated_at_utc=NOW,
    )


def test_real_typed_sources_are_reconciled_recomputed_and_can_pass(tmp_path) -> None:
    _, evidence, policy = _derive(tmp_path)
    payload = evidence.payload
    assert payload["prospective_prediction_count"] == 1
    assert payload["timestamped_quote_count"] == 3
    assert payload["completed_settlement_count"] == 1
    assert payload["realised_profit_loss"] == 0.5
    assert payload["realised_roi"] == 0.5
    assert {item["role"] for item in payload["upstream_artifacts"]} == {
        PREDICTIONS_ROLE,
        QUOTES_ROLE,
        RESULTS_ROLE,
        SETTLEMENTS_ROLE,
        MONITORING_ROLE,
    }
    decision = _decision(evidence, policy)
    assert decision.opportunity_analysis_eligible is True
    assert decision.bet_proposal_eligible is True
    assert decision.promotion_eligible is True


def test_request_accepts_only_role_bound_references_and_output() -> None:
    request = {
        "schema_version": "football-economic-evaluation-request-v1",
        "output_relative_directory": "economic/output",
        "upstream_artifacts": {
            role: [
                {
                    "relative_directory": f"upstream/{role}",
                    "artifact_id": f"{role}-artifact",
                    "checksum_sha256": "1" * 64,
                }
            ]
            for role in (
                PREDICTIONS_ROLE,
                QUOTES_ROLE,
                RESULTS_ROLE,
                SETTLEMENTS_ROLE,
                MONITORING_ROLE,
            )
        },
    }
    parsed = parse_economic_evaluation_request(json.dumps(request).encode())
    assert parsed.output_relative_directory == "economic/output"
    request["log_loss"] = 0.0
    with pytest.raises(EvaluationError, match="fields are not exact"):
        parse_economic_evaluation_request(json.dumps(request).encode())


def test_request_rejects_missing_duplicate_and_self_declared_role_contracts() -> None:
    base = {
        "schema_version": "football-economic-evaluation-request-v1",
        "output_relative_directory": "economic/output",
        "upstream_artifacts": {
            role: [
                {
                    "relative_directory": f"upstream/{role}",
                    "artifact_id": f"{role}-artifact",
                    "checksum_sha256": "1" * 64,
                }
            ]
            for role in (
                PREDICTIONS_ROLE,
                QUOTES_ROLE,
                RESULTS_ROLE,
                SETTLEMENTS_ROLE,
                MONITORING_ROLE,
            )
        },
    }
    del base["upstream_artifacts"][MONITORING_ROLE]
    with pytest.raises(EvaluationError, match="roles are not exact"):
        parse_economic_evaluation_request(json.dumps(base).encode())
    base["upstream_artifacts"][MONITORING_ROLE] = [
        {
            "relative_directory": "upstream/monitoring",
            "artifact_id": "monitoring-artifact",
            "checksum_sha256": "1" * 64,
            "artifact_type": "test-prospective-input",
        }
    ]
    with pytest.raises(EvaluationError, match="fields are not exact"):
        parse_economic_evaluation_request(json.dumps(base).encode())


def test_resigned_metric_tamper_is_detected_by_rederivation(tmp_path) -> None:
    artifact, _, _ = _derive(tmp_path)
    payload = dict(artifact.payload)
    payload["realised_roi"] = 99.0
    document = build_analytical_artifact_document(
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        payload=payload,
    )
    text = dumps_canonical_json(document) + "\n"
    manifest = tmp_path / "economic" / "manifest.json"
    manifest.write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode()).hexdigest()
    (tmp_path / "economic" / "manifest_checksum.sha256").write_text(checksum + "\n")
    with pytest.raises(ArtifactError, match="do not match verified upstream"):
        load_football_economic_evidence(root=tmp_path, relative_directory="economic")


def test_proposal_and_promotion_eligibility_are_independent(tmp_path) -> None:
    policy = _policy(minimum_realised_roi=0.75)
    _, evidence, _ = _derive(tmp_path, policy=policy)
    decision = _decision(evidence, policy)
    assert decision.bet_proposal_eligible is False
    assert decision.promotion_eligible is True
    assert "economic-return-threshold-failed" in decision.hold_reasons


@pytest.mark.parametrize(
    "baseline_field",
    (
        "market_baseline_log_loss",
        "market_baseline_brier_score",
        "market_baseline_rps",
    ),
)
def test_every_proper_score_baseline_is_evaluated(tmp_path, baseline_field) -> None:
    _, evidence, policy = _derive(tmp_path)
    payload = dict(evidence.payload)
    metric = {
        "market_baseline_log_loss": "log_loss",
        "market_baseline_brier_score": "multiclass_brier_score",
        "market_baseline_rps": "ranked_probability_score",
    }[baseline_field]
    payload[baseline_field] = float(payload[metric]) - 1.0
    forged = FootballEconomicEvidence(evidence.artifact, payload)
    decision = _decision(forged, policy)
    assert decision.promotion_eligible is False
    assert "market-baseline-threshold-failed" in decision.hold_reasons

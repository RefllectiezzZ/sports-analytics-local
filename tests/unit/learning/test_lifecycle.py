from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.core.exceptions import ModelError
from sports_analytics.learning.lifecycle import (
    ChampionHistory,
    ChampionRevision,
    RetrainingPolicy,
    TrainingEligibilityState,
    build_training_eligibility_ledger,
    evaluate_retraining_trigger,
    promote_challenger,
    rollback_champion,
)
from sports_analytics.results.contracts import (
    EventResultStatus,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import publish_result_snapshot

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _snapshot(tmp_path):
    result = build_football_full_match_1x2_result(
        canonical_event_id="event-1",
        scheduled_start_utc=NOW - timedelta(days=2),
        event_status=EventResultStatus.COMPLETED,
        source_name="verified-source",
        source_event_id="source-event-1",
        source_observed_at_utc=NOW - timedelta(days=1),
        source_checksum_sha256="a" * 64,
        result_provenance="verified-canonical-football-snapshot",
        home_canonical_participant_id="home",
        away_canonical_participant_id="away",
        full_time_home_score=2,
        full_time_away_score=1,
        result_timestamp_utc=NOW - timedelta(days=1),
    )
    return publish_result_snapshot(
        root=tmp_path,
        relative_directory="results/event-1",
        result=result,
    )


def test_verified_result_enters_later_training_ledger_without_prediction_leakage(
    tmp_path,
) -> None:
    snapshot = _snapshot(tmp_path)
    ledger = build_training_eligibility_ledger(
        result_snapshots=(snapshot,),
        event_competitions={"event-1": "eng-premier-league"},
        pre_match_feature_artifact_ids={"event-1": "feature-before-kickoff"},
        allowed_competitions=frozenset({"eng-premier-league"}),
        already_trained_event_ids=frozenset(),
        cutoff_utc=NOW,
    )
    assert ledger.records[0].state is TrainingEligibilityState.ELIGIBLE
    assert ledger.records[0].pre_match_feature_artifact_id == "feature-before-kickoff"
    assert "prediction" not in ledger.records[0].to_json()


def test_retraining_threshold_active_job_and_failed_cooldown() -> None:
    policy = RetrainingPolicy()
    base = {
        "policy": policy,
        "evaluated_at_utc": NOW,
        "champion_created_at_utc": NOW - timedelta(days=20),
        "last_successful_tournament_at_utc": NOW - timedelta(days=10),
        "season_transition_detected": False,
        "data_coverage": 1.0,
        "competition_count": 1,
    }
    below = evaluate_retraining_trigger(
        **base,
        eligible_new_matches=99,
        last_failed_cycle_at_utc=None,
        active_jobs_for_scope=0,
    )
    due = evaluate_retraining_trigger(
        **base,
        eligible_new_matches=100,
        last_failed_cycle_at_utc=None,
        active_jobs_for_scope=0,
    )
    active = evaluate_retraining_trigger(
        **base,
        eligible_new_matches=100,
        last_failed_cycle_at_utc=None,
        active_jobs_for_scope=1,
    )
    cooldown = evaluate_retraining_trigger(
        **base,
        eligible_new_matches=100,
        last_failed_cycle_at_utc=NOW - timedelta(days=1),
        active_jobs_for_scope=0,
    )
    assert not below.should_run
    assert due.should_run
    assert active.blocker_codes == ("retraining-job-already-active",)
    assert cooldown.blocker_codes == ("failed-retraining-cooldown-active",)


def test_champion_requires_explicit_strict_promotion_and_retains_rollback() -> None:
    first = ChampionRevision(
        revision=1,
        model_artifact_id="model-a",
        model_checksum_sha256="a" * 64,
        training_evidence_artifact_id="training-a",
        tournament_artifact_id="tournament-a",
        effective_at_utc=NOW - timedelta(days=10),
        action="initial",
        previous_model_artifact_id=None,
    )
    history = ChampionHistory("football:eng-premier-league", (first,))
    with pytest.raises(ModelError, match="production-evidence"):
        promote_challenger(
            history,
            challenger_model_artifact_id="model-b",
            challenger_checksum_sha256="b" * 64,
            training_evidence_artifact_id="training-b",
            tournament_artifact_id="tournament-b",
            promoted_at_utc=NOW,
            evidence_gate_state="insufficient-real-evaluation-data",
            compatible_scope=True,
            verified_artifact=True,
            confidence_intervals_valid=True,
            proper_score_improved=True,
            calibration_regressed=False,
            coverage_regressed=False,
            severe_competition_regression=False,
            rho_stable=True,
        )
    promoted = promote_challenger(
        history,
        challenger_model_artifact_id="model-b",
        challenger_checksum_sha256="b" * 64,
        training_evidence_artifact_id="training-b",
        tournament_artifact_id="tournament-b",
        promoted_at_utc=NOW,
        evidence_gate_state="production-eligible",
        compatible_scope=True,
        verified_artifact=True,
        confidence_intervals_valid=True,
        proper_score_improved=True,
        calibration_regressed=False,
        coverage_regressed=False,
        severe_competition_regression=False,
        rho_stable=True,
    )
    assert history.champion.model_artifact_id == "model-a"
    assert promoted.champion.model_artifact_id == "model-b"
    rolled_back = rollback_champion(
        promoted,
        target_model_artifact_id="model-a",
        rolled_back_at_utc=NOW + timedelta(minutes=1),
    )
    assert rolled_back.champion.model_artifact_id == "model-a"
    assert rolled_back.champion.action == "rollback"

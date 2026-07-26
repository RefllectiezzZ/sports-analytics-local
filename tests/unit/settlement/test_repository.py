from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.core.exceptions import SettlementConflictError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.operations import (
    ResultSnapshotRegistrationRepository,
    SettlementRepository,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.results.contracts import (
    EventResultStatus,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import publish_result_snapshot
from sports_analytics.settlement.contracts import settle_single
from sports_analytics.settlement.service import SettlementReport, publish_settlement_report
from sports_analytics.sports.football.markets import match_result_1x2_selection

AS_OF = datetime(2026, 7, 1, 20, tzinfo=UTC)
ARTIFACT_ID = "a" * 64
ARTIFACT_CHECKSUM = "b" * 64


def _result_snapshot(root, *, home_score: int, observed: datetime, suffix: str):
    result = build_football_full_match_1x2_result(
        canonical_event_id="event-1",
        scheduled_start_utc=AS_OF - timedelta(hours=2),
        event_status=EventResultStatus.COMPLETED,
        source_name="synthetic-results",
        source_event_id=f"source-event-{suffix}",
        source_observed_at_utc=observed,
        source_checksum_sha256=(suffix * 64)[:64],
        result_provenance="repository-test",
        home_canonical_participant_id="home",
        away_canonical_participant_id="away",
        full_time_home_score=home_score,
        full_time_away_score=1,
        result_timestamp_utc=AS_OF - timedelta(minutes=5),
    )
    return publish_result_snapshot(
        root=root,
        relative_directory=f"results/{suffix}",
        result=result,
    )


def _report(root, settlement, suffix: str) -> SettlementReport:
    run_id = content_addressed_id(
        identity_type="analytical-settlement-run-v1",
        payload={
            "source_artifact_id": ARTIFACT_ID,
            "source_artifact_checksum_sha256": ARTIFACT_CHECKSUM,
            "policy_id": settlement.policy_id,
            "policy_version": settlement.policy_version,
            "as_of_utc": format_utc_timestamp(settlement.settlement_as_of_utc),
            "settlement_ids": [settlement.settlement_id],
        },
    )
    return publish_settlement_report(
        root=root,
        relative_directory=f"settlements/{suffix}",
        report=SettlementReport(
            run_id=run_id,
            source_artifact_id=ARTIFACT_ID,
            source_artifact_checksum_sha256=ARTIFACT_CHECKSUM,
            policy_id=settlement.policy_id,
            policy_version=settlement.policy_version,
            as_of_utc=settlement.settlement_as_of_utc,
            settlements=(settlement,),
        ),
    )


def _settlement(snapshot, as_of: datetime):
    return settle_single(
        source_artifact_id=ARTIFACT_ID,
        source_artifact_checksum_sha256=ARTIFACT_CHECKSUM,
        opportunity_id="opportunity-1",
        canonical_event_id="event-1",
        selection=CanonicalSelectionIdentity.from_selection(match_result_1x2_selection("home")),
        decimal_odds=Decimal("2"),
        result_snapshot=snapshot,
        as_of_utc=as_of,
    )


def test_pending_can_advance_and_final_conflict_is_audited_without_overwrite(
    tmp_path,
) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    pending = _report(
        tmp_path,
        _settlement(None, AS_OF - timedelta(days=1)),
        "pending",
    )
    winning_snapshot = _result_snapshot(
        tmp_path,
        home_score=2,
        observed=AS_OF,
        suffix="c",
    )
    winning = _report(tmp_path, _settlement(winning_snapshot, AS_OF), "winning")
    losing_snapshot = _result_snapshot(
        tmp_path,
        home_score=0,
        observed=AS_OF + timedelta(hours=1),
        suffix="d",
    )
    losing = _report(
        tmp_path,
        _settlement(losing_snapshot, AS_OF + timedelta(hours=1)),
        "losing",
    )
    with connect_database(database) as connection:
        repository = SettlementRepository(connection)
        registrations = ResultSnapshotRegistrationRepository(connection)
        with transaction(connection):
            repository.persist_report(report=pending, actor="test", created_at=AS_OF)
        with transaction(connection):
            registrations.register(
                snapshot=winning_snapshot,
                registered_at=AS_OF,
                actor="test",
            )
            repository.persist_report(report=winning, actor="test", created_at=AS_OF)
        current = connection.execute(
            "SELECT status, version FROM current_analytical_settlements"
        ).fetchone()
        assert current is not None
        assert (current["status"], current["version"]) == ("win", 2)
        with transaction(connection):
            registrations.register(
                snapshot=losing_snapshot,
                registered_at=AS_OF + timedelta(hours=1),
                actor="test",
            )
        with pytest.raises(SettlementConflictError):
            with transaction(connection):
                repository.persist_report(
                    report=losing,
                    actor="test",
                    created_at=AS_OF + timedelta(hours=1),
                )
        with transaction(connection):
            assert (
                repository.record_conflicts(
                    report=losing,
                    actor="test",
                    occurred_at=AS_OF + timedelta(hours=1),
                )
                == 1
            )
        current = connection.execute(
            "SELECT status, version FROM current_analytical_settlements"
        ).fetchone()
        audit_count = connection.execute(
            """
            SELECT COUNT(*) FROM settlement_audit_events
            WHERE event_type = 'contradictory-evidence-rejected'
            """
        ).fetchone()[0]
        assert (current["status"], current["version"]) == ("win", 2)
        assert audit_count == 1


def test_current_nonfinal_state_advances_only_with_strictly_later_evidence(tmp_path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    first = _report(tmp_path, _settlement(None, AS_OF - timedelta(hours=2)), "pending-t1")
    second = _report(tmp_path, _settlement(None, AS_OF - timedelta(hours=1)), "pending-t2")
    stale = _report(
        tmp_path,
        _settlement(None, AS_OF - timedelta(minutes=90)),
        "pending-stale",
    )
    with connect_database(database) as connection:
        repository = SettlementRepository(connection)
        with transaction(connection):
            repository.persist_report(report=first, actor="test", created_at=AS_OF)
        with transaction(connection):
            repository.persist_report(report=second, actor="test", created_at=AS_OF)
        with pytest.raises(SettlementConflictError, match="stale"):
            with transaction(connection):
                repository.persist_report(report=stale, actor="test", created_at=AS_OF)
        current = connection.execute(
            "SELECT status, version FROM current_analytical_settlements"
        ).fetchone()
        assert current is not None
        assert (current["status"], current["version"]) == ("pending", 2)
        assert connection.execute("SELECT COUNT(*) FROM settlement_runs").fetchone()[0] == 2

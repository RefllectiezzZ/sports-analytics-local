from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.operations.handlers import run_monitoring_handler
from sports_analytics.results.contracts import (
    EventResultStatus,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import publish_result_snapshot
from sports_analytics.services.engine_cli import main as engine_main

AS_OF = datetime(2026, 6, 2, 20, tzinfo=UTC)


def test_result_snapshot_cli_success_and_failure_exit_codes(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "storage" / "snapshots"
    result = build_football_full_match_1x2_result(
        canonical_event_id="event-1",
        scheduled_start_utc=AS_OF - timedelta(hours=2),
        event_status=EventResultStatus.COMPLETED,
        source_name="synthetic-results",
        source_event_id="source-event-1",
        source_observed_at_utc=AS_OF,
        source_checksum_sha256="a" * 64,
        result_provenance="cli-test",
        home_canonical_participant_id="home",
        away_canonical_participant_id="away",
        full_time_home_score=1,
        full_time_away_score=0,
        result_timestamp_utc=AS_OF - timedelta(minutes=5),
    )
    snapshot = publish_result_snapshot(
        root=root,
        relative_directory="results/event-1",
        result=result,
    )
    assert (
        engine_main(
            [
                "--verify-result-snapshot",
                "results/event-1",
                "--checksum",
                snapshot.checksum_sha256,
            ]
        )
        == 0
    )
    assert '"verified"' not in capsys.readouterr().out
    assert (
        engine_main(
            [
                "--verify-result-snapshot",
                "results/event-1",
                "--checksum",
                "f" * 64,
            ]
        )
        == 2
    )
    assert "error:" in capsys.readouterr().err


def test_monitoring_worker_handler_is_idempotent(tmp_path) -> None:
    database = tmp_path / "storage" / "operational.sqlite3"
    exports = tmp_path / "storage" / "exports"
    exports.mkdir(parents=True)
    ensure_database_ready(database)
    context = JobExecutionContext(
        job_id="11111111-1111-4111-8111-111111111111",
        worker_id="22222222-2222-4222-8222-222222222222",
        attempt=1,
        maximum_attempts=3,
        claimed_at=AS_OF,
        lease_expires_at=AS_OF + timedelta(minutes=5),
        logger=logging.getLogger("test"),
    )
    object.__setattr__(context, "_database_path", database)
    object.__setattr__(context, "_exports_directory", exports)
    payload = {
        "policy": {
            "policy_id": "worker-monitoring",
            "policy_version": "monitoring-policy-v1",
            "thresholds": [
                {
                    "metric_name": "artifact_failure_count",
                    "warning": 1,
                    "critical": 2,
                    "direction": "higher-is-worse",
                }
            ],
        },
        "inputs": {
            "evidence": [
                {
                    "evidence_type": "snapshot",
                    "evidence_id": "snapshot-1",
                    "checksum_sha256": "a" * 64,
                }
            ],
            "artifact_failure_count": 0,
            "performance": [],
        },
        "as_of_utc": "2026-06-02T20:00:00.000000Z",
        "window_start_utc": "2026-06-01T20:00:00.000000Z",
        "window_end_utc": "2026-06-02T20:00:00.000000Z",
        "output_relative_directory": "monitoring/worker-replay",
    }
    first = run_monitoring_handler(context, payload)
    second = run_monitoring_handler(context, payload)
    assert first == second

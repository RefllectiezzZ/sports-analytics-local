"""Tests for the durable worker CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.jobs import cli
from sports_analytics.jobs.types import (
    LeaseRecoveryResult,
    QueueStatus,
    WorkerRunResult,
    WorkerStatus,
)


def test_formatters_are_stable() -> None:
    from datetime import UTC, datetime

    observed = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
    assert cli.format_queue_status(
        QueueStatus(
            pending_count=1,
            available_pending_count=2,
            delayed_pending_count=3,
            running_count=4,
            succeeded_count=5,
            failed_count=6,
            cancelled_count=7,
            expired_running_lease_count=8,
            active_worker_count=9,
            stale_worker_count=10,
            observed_at=observed,
        )
    ) == (
        "queue status: pending=1 available=2 delayed=3 running=4 expired=8 "
        "succeeded=5 failed=6 cancelled=7 workers_active=9 workers_stale=10"
    )
    assert (
        cli.format_lease_recovery(
            LeaseRecoveryResult(
                scanned_count=3,
                requeued_count=2,
                failed_count=1,
                requeued_job_ids=("a", "b"),
                failed_job_ids=("c",),
            )
        )
        == "lease recovery: scanned=3 requeued=2 failed=1"
    )
    assert (
        cli.format_worker_result(
            WorkerRunResult(
                worker_id="worker",
                jobs_processed=4,
                stop_reason="max_jobs",
                status=WorkerStatus.STOPPED,
            )
        )
        == "worker stopped: worker_id=worker jobs_processed=4 stop_reason=max_jobs status=stopped"
    )


def test_worker_once_mode_runs_without_jobs(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "--once",
            "--worker-id",
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "worker stopped:" in captured.out
    assert "stop_reason=once_no_job" in captured.out
    assert (isolated_cwd / "storage" / "operational.sqlite3").is_file()


def test_queue_status_is_read_only_for_existing_database(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = isolated_cwd / "storage" / "operational.sqlite3"
    ensure_database_ready(db)
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "storage/operational.sqlite3"\n',
        encoding="utf-8",
    )
    before = db.stat().st_mtime_ns
    code = cli.main(["--config", str(config), "--queue-status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "queue status:" in captured.out
    assert db.stat().st_mtime_ns == before


def test_recover_expired_leases_mode(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["--recover-expired-leases"])
    captured = capsys.readouterr()
    assert code == 0
    assert "lease recovery: scanned=0 requeued=0 failed=0" in captured.out


def test_invalid_worker_id_and_mutually_exclusive_modes(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["--once", "--worker-id", "not-a-uuid"])
    captured = capsys.readouterr()
    assert code == 2
    assert "invalid worker UUID" in captured.err

    with pytest.raises(SystemExit):
        cli.main(["--once", "--max-jobs", "1"])
    with pytest.raises(SystemExit):
        cli.main(["--queue-status", "--recover-expired-leases"])

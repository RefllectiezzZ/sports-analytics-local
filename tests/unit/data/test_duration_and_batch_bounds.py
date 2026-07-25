"""Regression tests for safe datetime arithmetic and recovery batch bounds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, run_component
from sports_analytics.core.exceptions import ConfigurationError, RepositoryError
from sports_analytics.core.settings import WorkerSettings, load_settings
from sports_analytics.core.validation import (
    MAX_DURATION_SECONDS,
    MAX_RECOVERY_BATCH_SIZE,
    add_duration,
    parse_positive_decimal_int,
    subtract_duration,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.jobs.backoff import compute_retry_available_at
from sports_analytics.jobs.types import WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
JOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PLUS_ONE_HOUR = timezone(timedelta(hours=1))


def test_add_and_subtract_duration_helpers() -> None:
    assert add_duration(FIXED, 30, field_name="seconds") == FIXED + timedelta(seconds=30)
    offset = FIXED.astimezone(PLUS_ONE_HOUR)
    added = add_duration(offset, 10, field_name="seconds")
    assert added.tzinfo is not None
    assert added == offset + timedelta(seconds=10)
    assert subtract_duration(FIXED, 5, field_name="seconds") == FIXED - timedelta(seconds=5)

    with pytest.raises(RepositoryError, match="timezone-aware"):
        add_duration(datetime(2026, 1, 1), 1, field_name="seconds")
    with pytest.raises(RepositoryError, match="timezone-aware"):
        subtract_duration(datetime(2026, 1, 1), 1, field_name="seconds")

    with pytest.raises(RepositoryError, match="overflows"):
        add_duration(datetime.max.replace(tzinfo=UTC), 1, field_name="seconds")
    with pytest.raises(RepositoryError, match="underflows"):
        subtract_duration(datetime.min.replace(tzinfo=UTC), 1, field_name="seconds")

    assert add_duration(FIXED, MAX_DURATION_SECONDS, field_name="seconds")
    with pytest.raises(RepositoryError):
        add_duration(FIXED, MAX_DURATION_SECONDS + 1, field_name="seconds")
    for value in (float("nan"), float("inf"), float("-inf"), 10**10000):
        with pytest.raises(RepositoryError):
            add_duration(FIXED, value, field_name="seconds")


def test_reconcile_stale_workers_near_datetime_min_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        workers = WorkerRepository(connection)
        with transaction(connection):
            worker = workers.register_worker(
                worker_id=WORKER_ID,
                name="local-worker",
                process_id=1,
                hostname="host",
                started_at=FIXED,
                capabilities={},
                status=WorkerStatus.RUNNING,
            )
            version_before = worker.version
            with pytest.raises(RepositoryError, match="underflows|stale_threshold"):
                workers.reconcile_stale_workers(
                    now=datetime.min.replace(tzinfo=UTC),
                    stale_threshold_seconds=1,
                    actor="supervisor",
                )
        unchanged = workers.get_worker(WORKER_ID)
        assert unchanged is not None
        assert unchanged.version == version_before
        assert unchanged.status is WorkerStatus.RUNNING


def test_claim_near_datetime_max_raises_without_mutation(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection, immediate=True):
            WorkerRepository(connection).register_worker(
                worker_id=WORKER_ID,
                name="local-worker",
                process_id=1,
                hostname="host",
                started_at=FIXED,
                capabilities={},
                status=WorkerStatus.RUNNING,
            )
            JobRepository(connection).create_job(
                job_id=JOB_ID,
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            with pytest.raises(RepositoryError, match="overflows|lease_duration"):
                JobQueueRepository(connection).claim_next_job(
                    worker_id=WORKER_ID,
                    claimed_at=datetime.max.replace(tzinfo=UTC),
                    lease_duration_seconds=1,
                    actor=WORKER_ID,
                )
            job = JobRepository(connection).get_job(JOB_ID)
            worker = WorkerRepository(connection).get_worker(WORKER_ID)
            assert job is not None and job.status.value == "pending"
            assert worker is not None and worker.current_job_id is None
            assert worker.version == 1


def test_retry_available_at_near_datetime_max_raises_project_error() -> None:
    with pytest.raises(RepositoryError, match="overflows"):
        compute_retry_available_at(
            failed_at=datetime.max.replace(tzinfo=UTC),
            attempts=1,
            base_seconds=1,
            max_seconds=1,
        )


def test_parse_positive_decimal_int_respects_recovery_batch_maximum() -> None:
    assert parse_positive_decimal_int(1, field_name="batch", maximum=MAX_RECOVERY_BATCH_SIZE) == 1
    assert (
        parse_positive_decimal_int(
            MAX_RECOVERY_BATCH_SIZE,
            field_name="batch",
            maximum=MAX_RECOVERY_BATCH_SIZE,
        )
        == MAX_RECOVERY_BATCH_SIZE
    )
    assert (
        parse_positive_decimal_int(
            str(MAX_RECOVERY_BATCH_SIZE),
            field_name="batch",
            maximum=MAX_RECOVERY_BATCH_SIZE,
        )
        == MAX_RECOVERY_BATCH_SIZE
    )
    rejected: list[object] = [
        MAX_RECOVERY_BATCH_SIZE + 1,
        str(MAX_RECOVERY_BATCH_SIZE + 1),
        10**100,
        "1" + "0" * 99,
        "1" * 5000,
        True,
        1.0,
        "01",
        "+1",
        " 1",
        "1e3",
        0,
        -1,
        "",
    ]
    for value in rejected:
        with pytest.raises(RepositoryError):
            parse_positive_decimal_int(
                value,
                field_name="batch",
                maximum=MAX_RECOVERY_BATCH_SIZE,
            )


def test_worker_settings_recovery_batch_size_bounds(tmp_path: Path) -> None:
    from pydantic import ValidationError

    settings = WorkerSettings(recovery_batch_size=MAX_RECOVERY_BATCH_SIZE)
    assert settings.recovery_batch_size == MAX_RECOVERY_BATCH_SIZE
    with pytest.raises(ValidationError):
        WorkerSettings(recovery_batch_size=MAX_RECOVERY_BATCH_SIZE + 1)

    loaded = load_settings(
        overrides={"worker": {"recovery_batch_size": MAX_RECOVERY_BATCH_SIZE}},
        environ={},
        base_directory=tmp_path,
    )
    assert loaded.worker.recovery_batch_size == MAX_RECOVERY_BATCH_SIZE

    with pytest.raises(ConfigurationError):
        load_settings(
            overrides={"worker": {"recovery_batch_size": MAX_RECOVERY_BATCH_SIZE + 1}},
            environ={},
            base_directory=tmp_path,
        )

    toml = tmp_path / "batch.toml"
    toml.write_text(
        '[application]\nenvironment = "test"\n'
        f"[worker]\nrecovery_batch_size = {MAX_RECOVERY_BATCH_SIZE}\n",
        encoding="utf-8",
    )
    from_toml = load_settings(config_path=toml, environ={}, base_directory=tmp_path)
    assert from_toml.worker.recovery_batch_size == MAX_RECOVERY_BATCH_SIZE

    env_file = tmp_path / ".env"
    env_file.write_text(
        f"SPORTS_ANALYTICS_WORKER__RECOVERY_BATCH_SIZE={MAX_RECOVERY_BATCH_SIZE}\n",
        encoding="utf-8",
    )
    from_env = load_settings(env_file=env_file, environ={}, base_directory=tmp_path)
    assert from_env.worker.recovery_batch_size == MAX_RECOVERY_BATCH_SIZE
    from_os = load_settings(
        environ={"SPORTS_ANALYTICS_WORKER__RECOVERY_BATCH_SIZE": str(MAX_RECOVERY_BATCH_SIZE)},
        base_directory=tmp_path,
    )
    assert from_os.worker.recovery_batch_size == MAX_RECOVERY_BATCH_SIZE


def test_validate_config_rejects_oversized_recovery_batch(
    tmp_path: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        f"[worker]\nrecovery_batch_size = {MAX_RECOVERY_BATCH_SIZE + 1}\n",
        encoding="utf-8",
    )
    code = run_component(
        "worker",
        "test",
        argv=["--validate-config", "--config", str(config)],
    )
    captured = capsys.readouterr()
    assert code == CONFIG_ERROR_EXIT
    assert "recovery_batch_size" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "storage").exists()


def test_recover_expired_leases_rejects_oversized_maximum_rows_before_sql(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection, immediate=True):
            WorkerRepository(connection).register_worker(
                worker_id=WORKER_ID,
                name="local-worker",
                process_id=1,
                hostname="host",
                started_at=FIXED,
                capabilities={},
                status=WorkerStatus.RUNNING,
            )
            before = WorkerRepository(connection).get_worker(WORKER_ID)
            assert before is not None
            with pytest.raises(RepositoryError, match="maximum_rows"):
                JobQueueRepository(connection).recover_expired_leases(
                    recovered_at=FIXED,
                    actor=WORKER_ID,
                    retry_backoff_base_seconds=1,
                    retry_backoff_max_seconds=1,
                    maximum_rows=MAX_RECOVERY_BATCH_SIZE + 1,
                )
            after = WorkerRepository(connection).get_worker(WORKER_ID)
            assert after is not None
            assert after.version == before.version
            assert after.status is WorkerStatus.RUNNING

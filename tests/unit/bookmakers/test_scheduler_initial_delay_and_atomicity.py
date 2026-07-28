"""Scheduler first-cycle anchoring and atomic enqueue tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sports_analytics.bookmakers.scheduler import BookmakerScheduler
from sports_analytics.bookmakers.scheduler_ops import (
    atomic_enqueue_autonomous_cycle,
    ensure_scheduler_anchor,
)
from sports_analytics.bookmakers.types import BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER
from sports_analytics.core.settings import (
    BookmakerProviderSettings,
    BookmakersSettings,
    Settings,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.repositories.jobs import JobRepository

T0 = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def _settings(*, initial_delay_seconds: int = 30) -> Settings:
    provider = BookmakerProviderSettings(
        enabled=True,
        acquisition_interval_seconds=300,
        initial_delay_seconds=initial_delay_seconds,
        blocked_cooldown_seconds=900,
    )
    return Settings(
        bookmakers=BookmakersSettings(
            enabled=True,
            betano=provider,
            betclic=BookmakerProviderSettings(
                enabled=False,
                acquisition_interval_seconds=300,
                initial_delay_seconds=initial_delay_seconds,
                blocked_cooldown_seconds=900,
            ),
        )
    )


def test_t0_creates_anchor_and_enqueues_nothing(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    clock = {"now": T0}

    scheduler = BookmakerScheduler(
        settings=_settings(initial_delay_seconds=30),
        database_path=database,
        clock=lambda: clock["now"],
        sleeper=lambda _: None,
        install_signals=False,
    )
    assert scheduler.tick() == 0
    with connect_database(database, read_only=True) as connection:
        anchors = connection.execute(
            "SELECT COUNT(*) AS n FROM bookmaker_scheduler_anchors"
        ).fetchone()
        jobs = connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    assert int(anchors["n"]) == 3
    assert int(jobs["n"]) == 0


def test_t0_plus_29s_still_enqueues_nothing(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    clock = {"now": T0}
    scheduler = BookmakerScheduler(
        settings=_settings(initial_delay_seconds=30),
        database_path=database,
        clock=lambda: clock["now"],
        sleeper=lambda _: None,
        install_signals=False,
    )
    scheduler.tick()
    clock["now"] = T0 + timedelta(seconds=29)
    assert scheduler.tick() == 0


def test_t0_plus_30s_enqueues_exactly_once_per_sport(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    clock = {"now": T0}
    scheduler = BookmakerScheduler(
        settings=_settings(initial_delay_seconds=30),
        database_path=database,
        clock=lambda: clock["now"],
        sleeper=lambda _: None,
        install_signals=False,
    )
    scheduler.tick()
    clock["now"] = T0 + timedelta(seconds=30)
    assert scheduler.tick() == 3
    clock["now"] = T0 + timedelta(seconds=31)
    assert scheduler.tick() == 0


def test_restart_before_due_preserves_anchor(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    bookmakers = _settings(initial_delay_seconds=30).bookmakers
    first = ensure_scheduler_anchor(
        database_path=database,
        bookmakers=bookmakers,
        sport="football",
        now=T0,
    )
    second = ensure_scheduler_anchor(
        database_path=database,
        bookmakers=bookmakers,
        sport="football",
        now=T0 + timedelta(seconds=10),
    )
    assert first.first_due_at == second.first_due_at == T0 + timedelta(seconds=30)


def test_restart_after_due_enqueues_missed_slot(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    bookmakers = _settings(initial_delay_seconds=0).bookmakers
    due = T0
    result = atomic_enqueue_autonomous_cycle(
        database_path=database,
        bookmakers=bookmakers,
        sport="football",
        scheduled_for=due,
        now=T0,
    )
    assert result.inserted is True
    replay = atomic_enqueue_autonomous_cycle(
        database_path=database,
        bookmakers=bookmakers,
        sport="football",
        scheduled_for=due,
        now=T0 + timedelta(hours=1),
    )
    assert replay.inserted is False
    assert replay.cycle_id == result.cycle_id


def test_atomic_enqueue_rollback_leaves_no_job_or_cycle(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    bookmakers = _settings(initial_delay_seconds=0).bookmakers
    with patch(
        "sports_analytics.bookmakers.scheduler_ops.BookmakerRepository.insert_scheduler_cycle",
        side_effect=RuntimeError("injected failure after job create"),
    ):
        with pytest.raises(RuntimeError, match="injected failure"):
            atomic_enqueue_autonomous_cycle(
                database_path=database,
                bookmakers=bookmakers,
                sport="football",
                scheduled_for=T0,
                now=T0,
            )
    with connect_database(database, read_only=True) as connection:
        jobs = connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        cycles = connection.execute(
            "SELECT COUNT(*) AS n FROM bookmaker_scheduler_cycles"
        ).fetchone()
    assert int(jobs["n"]) == 0
    assert int(cycles["n"]) == 0


def test_failure_after_cycle_insert_is_idempotent_on_replay(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    bookmakers = _settings(initial_delay_seconds=0).bookmakers
    with connect_database(database) as connection:
        with transaction(connection, immediate=True):
            jobs = JobRepository(connection)
            job = jobs.create_job(
                job_type="ingest.bookmaker-autonomous-cycle",
                payload={"sport": "football", "acquisition_cycle_id": "x", "observed_at_utc": None},
                maximum_attempts=2,
                actor="test",
                created_at=T0,
                idempotency_key="bookmaker-auto:football:20260726T120000Z",
            )
            repo = BookmakerRepository(connection)
            cycle_id, inserted = repo.insert_scheduler_cycle(
                provider_id=BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER,
                sport="football",
                scheduled_for=T0,
                enqueued_at=T0,
                job_id=job.id,
            )
    assert inserted is True
    replay = atomic_enqueue_autonomous_cycle(
        database_path=database,
        bookmakers=bookmakers,
        sport="football",
        scheduled_for=T0,
        now=T0,
    )
    assert replay.inserted is False
    assert replay.cycle_id == cycle_id

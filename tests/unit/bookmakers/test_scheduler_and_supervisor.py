"""Scheduler duplicate suppression and run_local bookmaker supervision."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.scheduler import BookmakerScheduler
from sports_analytics.core.exceptions import WorkerError
from sports_analytics.core.settings import (
    BookmakerProviderSettings,
    BookmakersSettings,
    Settings,
)
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.local.supervisor import LocalSupervisor
from tests.unit.local.test_supervisor import FakeChild, FakePopenFactory

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _settings(*, enabled: bool = True) -> Settings:
    provider = BookmakerProviderSettings(
        enabled=True,
        acquisition_interval_seconds=300,
        initial_delay_seconds=0,
        blocked_cooldown_seconds=900,
    )
    return Settings(
        bookmakers=BookmakersSettings(
            enabled=enabled,
            betano=provider,
            betclic=BookmakerProviderSettings(
                enabled=False,
                acquisition_interval_seconds=300,
                initial_delay_seconds=0,
                blocked_cooldown_seconds=900,
            ),
        )
    )


def test_scheduler_enqueues_then_suppresses_duplicates_with_fake_clock(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    clock = {"now": NOW}

    def fake_clock() -> datetime:
        return clock["now"]

    scheduler = BookmakerScheduler(
        settings=_settings(enabled=True),
        database_path=database,
        clock=fake_clock,
        sleeper=lambda _: None,
        install_signals=False,
    )
    first = scheduler.tick()
    assert first == 3  # football, basketball, tennis
    second = scheduler.tick()
    assert second == 0
    with connect_database(database, read_only=True) as connection:
        cycles = connection.execute(
            "SELECT COUNT(*) AS n FROM bookmaker_scheduler_cycles"
        ).fetchone()
        jobs = connection.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    assert int(cycles["n"]) == 3
    assert int(jobs["n"]) == 3


def test_scheduler_disabled_does_not_enqueue(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    scheduler = BookmakerScheduler(
        settings=_settings(enabled=False),
        database_path=database,
        clock=lambda: NOW,
        sleeper=lambda _: None,
        install_signals=False,
    )
    assert scheduler.tick() == 0
    assert scheduler.run_forever() == 0


def _write_config(isolated_cwd: Path, *, bookmakers_enabled: bool) -> Path:
    enabled = "true" if bookmakers_enabled else "false"
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "storage/operational.sqlite3"\n'
        f"[bookmakers]\nenabled = {enabled}\n",
        encoding="utf-8",
    )
    return config


def test_supervisor_starts_worker_and_scheduler_when_enabled(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd, bookmakers_enabled=True)
    worker = FakeChild(exit_code=0)
    scheduler = FakeChild(exit_code=0)
    popen = FakePopenFactory(children=[worker, scheduler])
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    code = runner.run(config=str(config), worker_once=False)
    assert code == 0
    assert len(popen.commands) == 2
    assert any("worker.py" in " ".join(cmd) for cmd in popen.commands)
    assert any("sports_analytics.bookmakers.scheduler" in " ".join(cmd) for cmd in popen.commands)


def test_supervisor_skips_scheduler_when_disabled_or_worker_once(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    disabled = _write_config(isolated_cwd, bookmakers_enabled=False)
    worker = FakeChild(exit_code=0)
    popen = FakePopenFactory(worker)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    assert runner.run(config=str(disabled), worker_once=False) == 0
    assert len(popen.commands) == 1

    enabled = _write_config(isolated_cwd, bookmakers_enabled=True)
    once_worker = FakeChild(exit_code=0)
    once_popen = FakePopenFactory(once_worker)
    once_runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=once_popen,
        install_signals=False,
        platform_name="linux",
    )
    assert once_runner.run(config=str(enabled), worker_once=True) == 0
    assert len(once_popen.commands) == 1
    assert "--once" in once_popen.commands[0]


def test_supervisor_child_crash_surfaces_and_stops_siblings(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd, bookmakers_enabled=True)
    worker = FakeChild(exit_code=9)
    scheduler = FakeChild(exit_code=0, timeout_once=True)
    popen = FakePopenFactory(children=[worker, scheduler])
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
        poll_interval_seconds=0.01,
    )
    code = runner.run(config=str(config), worker_once=False)
    assert code == 9
    assert scheduler.terminated == 1 or scheduler.killed == 1 or scheduler.poll() is not None


def test_supervisor_scheduler_startup_failure_surfaces(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd, bookmakers_enabled=True)
    worker = FakeChild(exit_code=0)

    class BoomFactory(FakePopenFactory):
        def __call__(
            self,
            command: Sequence[str],
            *,
            creationflags: int = 0,
        ) -> FakeChild:
            self.commands.append(list(command))
            self.creationflags.append(creationflags)
            if len(self.commands) == 1:
                return worker
            raise OSError("scheduler exec failed")

    popen = BoomFactory(worker)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(WorkerError, match="failed to start"):
        runner.run(config=str(config), worker_once=False)
    assert worker.terminated == 1 or worker.killed == 1 or worker.poll() is not None

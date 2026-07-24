"""Tests for the local worker process supervisor."""

from __future__ import annotations

import signal
import subprocess
import threading
from pathlib import Path

import pytest

from sports_analytics.local import supervisor
from sports_analytics.local.supervisor import LocalSupervisor, WindowsShutdownStrategy

WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeChild:
    def __init__(self, *, exit_code: int = 0, timeout_once: bool = False) -> None:
        self.pid = 1234
        self.exit_code = exit_code
        self.timeout_once = timeout_once
        self.terminated = 0
        self.killed = 0
        self.sent_signals: list[int] = []
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return None if self.terminated == 0 and self.killed == 0 else self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)
        return self.exit_code

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1
        self.exit_code = -9

    def send_signal(self, sig: int) -> None:
        self.sent_signals.append(sig)


class FakePopenFactory:
    def __init__(self, child: FakeChild) -> None:
        self.child = child
        self.commands: list[list[str]] = []
        self.creationflags: list[int] = []

    def __call__(self, command, *, creationflags: int = 0) -> FakeChild:
        self.commands.append(list(command))
        self.creationflags.append(creationflags)
        return self.child


def test_supervisor_run_bootstraps_database_and_starts_worker_with_absolute_paths(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = isolated_cwd / "settings.toml"
    env_file = isolated_cwd / ".env"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "storage/operational.sqlite3"\n',
        encoding="utf-8",
    )
    env_file.write_text("SPORTS_ANALYTICS_LOGGING__LEVEL=INFO\n", encoding="utf-8")
    child = FakeChild(exit_code=7)
    popen = FakePopenFactory(child)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )

    code = runner.run(
        config=str(config),
        env_file=str(env_file),
        worker_once=True,
        worker_max_jobs=3,
        worker_id=WORKER_ID.upper(),
    )

    assert code == 7
    assert (isolated_cwd / "storage" / "operational.sqlite3").is_file()
    assert popen.creationflags == [0]
    command = popen.commands[0]
    assert command[1] == str((isolated_cwd / "worker.py").resolve())
    assert command[command.index("--config") + 1] == str(config.resolve())
    assert command[command.index("--env-file") + 1] == str(env_file.resolve())
    assert "--once" in command
    assert command[command.index("--max-jobs") + 1] == "3"
    assert command[command.index("--worker-id") + 1] == WORKER_ID


def test_shutdown_child_uses_grace_then_kill_and_sends_grace_once() -> None:
    graceful = FakeChild(exit_code=0)
    runner = LocalSupervisor(install_signals=False, platform_name="linux")
    assert runner._shutdown_child(graceful, shutdown_grace_seconds=1) == 0
    assert graceful.terminated == 1
    assert graceful.killed == 0
    assert runner._shutdown_child(graceful, shutdown_grace_seconds=1) == 0
    assert graceful.terminated == 1

    stubborn = FakeChild(exit_code=0, timeout_once=True)
    runner = LocalSupervisor(install_signals=False, platform_name="linux")
    assert runner._shutdown_child(stubborn, shutdown_grace_seconds=1) == -9
    assert stubborn.terminated == 1
    assert stubborn.killed == 1


def test_posix_shutdown_strategy_uses_terminate() -> None:
    child = FakeChild(exit_code=0)
    runner = LocalSupervisor(install_signals=False, platform_name="linux")

    assert runner._shutdown_child(child, shutdown_grace_seconds=1) == 0

    assert child.terminated == 1
    assert child.sent_signals == []


def test_windows_shutdown_strategy_uses_creationflags_and_ctrl_break(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 21, raising=False)
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "storage/operational.sqlite3"\n',
        encoding="utf-8",
    )
    child = FakeChild(exit_code=0)
    popen = FakePopenFactory(child)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="win32",
    )

    assert runner.run(config=str(config)) == 0
    assert popen.creationflags == [512]

    stopping = FakeChild(exit_code=0)
    windows_runner = LocalSupervisor(
        install_signals=False,
        shutdown_strategy=WindowsShutdownStrategy(),
    )
    assert windows_runner._shutdown_child(stopping, shutdown_grace_seconds=1) == 0
    assert stopping.sent_signals == [21]
    assert stopping.terminated == 0


def test_signal_handler_only_sets_event(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, object] = {}

    def fake_getsignal(signum: int) -> object:
        return f"old-{signum}"

    def fake_signal(signum: int, handler: object) -> object:
        installed[signum] = handler
        return handler

    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal, "signal", fake_signal)
    stop_requested = threading.Event()
    runner = LocalSupervisor(install_signals=True)

    originals = runner._install_signal_handlers(stop_requested)

    assert originals
    assert not stop_requested.is_set()
    signum, handler = next(iter(installed.items()))
    assert callable(handler)
    handler(signum, None)
    assert stop_requested.is_set()


def test_build_worker_command_omits_absent_options() -> None:
    worker_script = Path("/tmp/worker.py")
    runner = LocalSupervisor(worker_script=worker_script, install_signals=False)
    command = runner._build_worker_command(
        config=None,
        env_file=None,
        worker_once=False,
        worker_max_jobs=None,
        worker_id=None,
    )
    assert command[-1] == str(worker_script.resolve())
    assert "--config" not in command
    assert "--once" not in command


def test_supervisor_main_reports_invalid_worker_id(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys,
) -> None:
    code = supervisor.main(["--worker-id", "not-a-uuid"])
    captured = capsys.readouterr()
    assert code == 2
    assert "invalid worker UUID" in captured.err

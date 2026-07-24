"""Tests for the local worker process supervisor."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sports_analytics.local import supervisor
from sports_analytics.local.supervisor import LocalSupervisor

WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeChild:
    def __init__(self, *, exit_code: int = 0, timeout_once: bool = False) -> None:
        self.exit_code = exit_code
        self.timeout_once = timeout_once
        self.terminated = 0
        self.killed = 0
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


class FakePopenFactory:
    def __init__(self, child: FakeChild) -> None:
        self.child = child
        self.commands: list[list[str]] = []

    def __call__(self, command) -> FakeChild:
        self.commands.append(list(command))
        return self.child


def test_supervisor_run_migrates_database_and_starts_worker_with_absolute_paths(
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
    command = popen.commands[0]
    assert command[1] == str((isolated_cwd / "worker.py").resolve())
    assert command[command.index("--config") + 1] == str(config.resolve())
    assert command[command.index("--env-file") + 1] == str(env_file.resolve())
    assert "--once" in command
    assert command[command.index("--max-jobs") + 1] == "3"
    assert command[command.index("--worker-id") + 1] == WORKER_ID


def test_terminate_child_uses_grace_then_kill() -> None:
    graceful = FakeChild(exit_code=0)
    assert LocalSupervisor._terminate_child(graceful, shutdown_grace_seconds=1) == 0
    assert graceful.terminated == 1
    assert graceful.killed == 0

    stubborn = FakeChild(exit_code=0, timeout_once=True)
    assert LocalSupervisor._terminate_child(stubborn, shutdown_grace_seconds=1) == -9
    assert stubborn.terminated == 1
    assert stubborn.killed == 1


def test_build_worker_command_omits_absent_options() -> None:
    runner = LocalSupervisor(worker_script="/tmp/worker.py", install_signals=False)
    command = runner._build_worker_command(
        config=None,
        env_file=None,
        worker_once=False,
        worker_max_jobs=None,
        worker_id=None,
    )
    assert command[-1] == "/tmp/worker.py"
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

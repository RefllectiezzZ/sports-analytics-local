"""Tests for the local worker process supervisor."""

from __future__ import annotations

import signal
import subprocess
import threading
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import WorkerError
from sports_analytics.local import supervisor
from sports_analytics.local.supervisor import LocalSupervisor, WindowsShutdownStrategy

WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeChild:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        timeout_once: bool = False,
        wait_error: Exception | None = None,
        terminate_error: Exception | None = None,
        send_signal_error: Exception | None = None,
        kill_error: Exception | None = None,
    ) -> None:
        self.pid = 1234
        self.exit_code = exit_code
        self.timeout_once = timeout_once
        self.wait_error = wait_error
        self.terminate_error = terminate_error
        self.send_signal_error = send_signal_error
        self.kill_error = kill_error
        self.terminated = 0
        self.killed = 0
        self.sent_signals: list[int] = []
        self.wait_calls: list[float | None] = []
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.wait_error is not None:
            error = self.wait_error
            self.wait_error = None
            raise error
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout)
        self._alive = False
        return self.exit_code

    def terminate(self) -> None:
        self.terminated += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        self._alive = False

    def kill(self) -> None:
        self.killed += 1
        if self.kill_error is not None:
            raise self.kill_error
        self._alive = False
        self.exit_code = -9

    def send_signal(self, sig: int) -> None:
        self.sent_signals.append(sig)
        if self.send_signal_error is not None:
            raise self.send_signal_error
        self._alive = False


class FakePopenFactory:
    def __init__(
        self,
        child: FakeChild | None = None,
        *,
        children: list[FakeChild] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.child = child
        self.children = list(children or [])
        self.error = error
        self.commands: list[list[str]] = []
        self.creationflags: list[int] = []
        self._index = 0

    def __call__(self, command, *, creationflags: int = 0) -> FakeChild:
        self.commands.append(list(command))
        self.creationflags.append(creationflags)
        if self.error is not None:
            raise self.error
        if self.children:
            child = self.children[self._index]
            self._index += 1
            return child
        assert self.child is not None
        return self.child


def _write_config(isolated_cwd: Path) -> Path:
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "storage/operational.sqlite3"\n',
        encoding="utf-8",
    )
    return config


def test_supervisor_run_bootstraps_database_and_starts_worker_with_absolute_paths(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    env_file = isolated_cwd / ".env"
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
    config = _write_config(isolated_cwd)
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


def test_supervisor_reuses_instance_and_sends_graceful_stop_per_run(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    first = FakeChild(exit_code=0, timeout_once=True)
    second = FakeChild(exit_code=0, timeout_once=True)
    # First wait times out once in the main loop so we can set stop via graceful path:
    # Use children that exit immediately on first wait for run completion after stop.
    first = FakeChild(exit_code=11)
    second = FakeChild(exit_code=22)
    popen = FakePopenFactory(children=[first, second])
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )

    assert runner.run(config=str(config)) == 11
    assert runner._graceful_stop_sent is False
    assert runner.run(config=str(config)) == 22
    assert len(popen.commands) == 2

    # Direct shutdown on a reused instance still resets per run via run(), and
    # _shutdown_child sends exactly one graceful stop per reset cycle.
    stopping_a = FakeChild(exit_code=0)
    stopping_b = FakeChild(exit_code=0)
    runner._graceful_stop_sent = False
    assert runner._shutdown_child(stopping_a, shutdown_grace_seconds=1) == 0
    assert stopping_a.terminated == 1
    runner._graceful_stop_sent = False
    assert runner._shutdown_child(stopping_b, shutdown_grace_seconds=1) == 0
    assert stopping_b.terminated == 1


def test_popen_creation_failure_raises_worker_error(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    popen = FakePopenFactory(error=OSError("cannot exec"))
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(WorkerError, match="failed to start worker child"):
        runner.run(config=str(config))


def test_unexpected_wait_error_cleans_up_child(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0, wait_error=RuntimeError("wait broke"))
    popen = FakePopenFactory(child)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(RuntimeError, match="wait broke"):
        runner.run(config=str(config))
    assert child.terminated == 1 or child.killed == 1
    assert child.poll() is not None


def test_graceful_signal_errors_trigger_cleanup(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0, terminate_error=OSError("terminate failed"))
    # Force the main loop into shutdown by raising from wait after stop would be sent.
    # Exercise _shutdown_child graceful failure path directly.
    runner = LocalSupervisor(install_signals=False, platform_name="linux")
    with pytest.raises(OSError, match="terminate failed"):
        runner._shutdown_child(child, shutdown_grace_seconds=1)

    windows_child = FakeChild(exit_code=0, send_signal_error=OSError("ctrl-break failed"))
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 21, raising=False)
    windows_runner = LocalSupervisor(
        install_signals=False,
        shutdown_strategy=WindowsShutdownStrategy(),
    )
    with pytest.raises(OSError, match="ctrl-break failed"):
        windows_runner._shutdown_child(windows_child, shutdown_grace_seconds=1)

    # Cleanup path after KeyboardInterrupt still kills.
    interrupt_child = FakeChild(exit_code=0, wait_error=KeyboardInterrupt())
    popen = FakePopenFactory(interrupt_child)
    loop_runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(KeyboardInterrupt):
        loop_runner.run(config=str(config))
    assert interrupt_child.poll() is not None

    exit_child = FakeChild(exit_code=0, wait_error=SystemExit(3))
    popen2 = FakePopenFactory(exit_child)
    exit_runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen2,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(SystemExit):
        exit_runner.run(config=str(config))
    assert exit_child.poll() is not None


def test_cleanup_failure_preserves_primary_exception(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(
        exit_code=0,
        wait_error=RuntimeError("primary wait failure"),
        timeout_once=True,
        terminate_error=OSError("terminate failed"),
        kill_error=OSError("kill failed"),
    )
    popen = FakePopenFactory(child)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(RuntimeError, match="primary wait failure") as exc_info:
        runner.run(config=str(config))
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("child cleanup also failed" in note for note in notes)


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


def test_supervisor_main_reports_popen_failure_without_traceback(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    config = _write_config(isolated_cwd)

    def boom(command, *, creationflags: int = 0):
        del command, creationflags
        raise OSError("cannot exec")

    monkeypatch.setattr(supervisor, "_default_popen", boom)
    code = supervisor.main(["--config", str(config)])
    captured = capsys.readouterr()
    assert code == 2
    assert "failed to start worker child" in captured.err
    assert "Traceback" not in captured.err


class _SignalBoard:
    """Deterministic fake signal registry for installation/restoration tests."""

    def __init__(self, *, fail_install_on_count: int | None = None) -> None:
        self.handlers: dict[int, object] = {}
        self.install_count = 0
        self.restore_attempts: list[int] = []
        self.fail_install_on_count = fail_install_on_count
        self.fail_restore_on: set[int] = set()
        self.fail_all_restores = False

    def getsignal(self, signum: int) -> object:
        return self.handlers.get(signum, f"original-{signum}")

    def signal(self, signum: int, handler: object) -> object:
        previous = self.getsignal(signum)
        if callable(handler):
            self.install_count += 1
            if (
                self.fail_install_on_count is not None
                and self.install_count == self.fail_install_on_count
            ):
                raise OSError("signal install failed")
        else:
            self.restore_attempts.append(signum)
            if self.fail_all_restores or signum in self.fail_restore_on:
                raise OSError(f"restore failed for {signum}")
        self.handlers[signum] = handler
        return previous


def test_partial_signal_installation_failure_restores_and_cleans_child(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0)
    popen = FakePopenFactory(child)
    board = _SignalBoard(fail_install_on_count=2)
    monkeypatch.setattr(signal, "getsignal", board.getsignal)
    monkeypatch.setattr(signal, "signal", board.signal)
    monkeypatch.setattr(signal, "SIGBREAK", None, raising=False)

    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=True,
        platform_name="linux",
    )
    with pytest.raises(OSError, match="signal install failed"):
        runner.run(config=str(config))

    assert board.install_count == 2
    assert board.handlers.get(signal.SIGINT) == f"original-{signal.SIGINT}"
    assert child.terminated == 1 or child.killed == 1
    assert child.poll() is not None


def test_signal_installation_fails_before_any_handler_still_cleans_child(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0)
    popen = FakePopenFactory(child)
    board = _SignalBoard(fail_install_on_count=1)
    monkeypatch.setattr(signal, "getsignal", board.getsignal)
    monkeypatch.setattr(signal, "signal", board.signal)

    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=True,
        platform_name="linux",
    )
    with pytest.raises(OSError, match="signal install failed"):
        runner.run(config=str(config))

    assert board.handlers == {}
    assert child.poll() is not None


def test_signal_restoration_failure_after_normal_exit_raises_worker_error(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0)
    popen = FakePopenFactory(child)
    board = _SignalBoard()
    board.fail_all_restores = True
    monkeypatch.setattr(signal, "getsignal", board.getsignal)
    monkeypatch.setattr(signal, "signal", board.signal)
    monkeypatch.setattr(signal, "SIGBREAK", None, raising=False)

    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=True,
        platform_name="linux",
    )
    with pytest.raises(WorkerError, match="failed to restore signal handlers"):
        runner.run(config=str(config))
    assert child.poll() is not None
    assert board.restore_attempts


def test_signal_restoration_failure_preserves_wait_exception_and_continues(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0, wait_error=RuntimeError("primary wait failure"))
    popen = FakePopenFactory(child)
    board = _SignalBoard()
    first_restore: int | None = None

    def selective_signal(signum: int, handler: object) -> object:
        nonlocal first_restore
        previous = board.getsignal(signum)
        if callable(handler):
            board.handlers[signum] = handler
            board.install_count += 1
            return previous
        board.restore_attempts.append(signum)
        if first_restore is None:
            first_restore = signum
            raise OSError("first restore failed")
        board.handlers[signum] = handler
        return previous

    monkeypatch.setattr(signal, "getsignal", board.getsignal)
    monkeypatch.setattr(signal, "signal", selective_signal)
    monkeypatch.setattr(signal, "SIGBREAK", None, raising=False)

    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=True,
        platform_name="linux",
    )
    with pytest.raises(RuntimeError, match="primary wait failure") as exc_info:
        runner.run(config=str(config))
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("signal handler restoration also failed" in note for note in notes)
    assert len(board.restore_attempts) >= 2
    assert child.poll() is not None


def test_cleanup_keyboardinterrupt_does_not_replace_primary(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0, wait_error=RuntimeError("primary wait failure"))
    child.terminate_error = KeyboardInterrupt()
    child.kill_error = KeyboardInterrupt()
    popen = FakePopenFactory(child)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(RuntimeError, match="primary wait failure"):
        runner.run(config=str(config))


def test_cleanup_systemexit_does_not_replace_primary(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    child = FakeChild(exit_code=0, wait_error=RuntimeError("primary wait failure"))
    child.terminate_error = SystemExit(9)
    child.kill_error = SystemExit(9)
    popen = FakePopenFactory(child)
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    with pytest.raises(RuntimeError, match="primary wait failure"):
        runner.run(config=str(config))


def test_supervisor_remains_reusable_after_completed_run(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    first = FakeChild(exit_code=1)
    second = FakeChild(exit_code=2)
    popen = FakePopenFactory(children=[first, second])
    runner = LocalSupervisor(
        worker_script=isolated_cwd / "worker.py",
        popen_factory=popen,
        install_signals=False,
        platform_name="linux",
    )
    assert runner.run(config=str(config)) == 1
    assert first.poll() is not None
    assert runner.run(config=str(config)) == 2
    assert second.poll() is not None
    assert runner._graceful_stop_sent is False

"""Local worker process supervisor."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import Protocol, cast

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    RepositoryError,
    RuntimeBootstrapError,
    WorkerError,
)
from sports_analytics.core.runtime import bootstrap_runtime
from sports_analytics.core.settings import WorkerSettings
from sports_analytics.data.types import normalize_uuid, validate_positive_finite_number

SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


class ChildProcess(Protocol):
    """Subset of ``subprocess.Popen`` used by the supervisor."""

    @property
    def pid(self) -> int | None:
        """Return the child process id when available."""

    def poll(self) -> int | None:
        """Return the child exit code if it has exited."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for child exit and return its exit code."""

    def terminate(self) -> None:
        """Request forced termination (platform-specific fallback)."""

    def kill(self) -> None:
        """Force child termination."""

    def send_signal(self, sig: int) -> None:
        """Deliver ``sig`` to the child process."""


class PopenFactory(Protocol):
    """Factory used to start the worker child process."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        creationflags: int = 0,
    ) -> ChildProcess:
        """Start ``command`` and return a child-process handle."""


def _default_popen(command: Sequence[str], *, creationflags: int = 0) -> ChildProcess:
    return cast(
        ChildProcess,
        subprocess.Popen(list(command), shell=False, creationflags=creationflags),
    )


class ProcessShutdownStrategy(Protocol):
    """Platform-specific child process start and graceful-stop strategy."""

    def creationflags(self) -> int:
        """Return subprocess creation flags for the child."""

    def request_graceful_stop(self, child: ChildProcess) -> None:
        """Ask the child to shut down cooperatively."""


class PosixShutdownStrategy:
    """POSIX graceful stop via SIGTERM / ``terminate()``."""

    def creationflags(self) -> int:
        return 0

    def request_graceful_stop(self, child: ChildProcess) -> None:
        child.terminate()


class WindowsShutdownStrategy:
    """Windows graceful stop via CTRL_BREAK_EVENT in a new process group."""

    def creationflags(self) -> int:
        return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    def request_graceful_stop(self, child: ChildProcess) -> None:
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is None:
            child.terminate()
            return
        child.send_signal(int(ctrl_break))


def select_shutdown_strategy(*, platform_name: str | None = None) -> ProcessShutdownStrategy:
    """Return the shutdown strategy for the current or injected platform."""
    name = sys.platform if platform_name is None else platform_name
    if name.startswith("win"):
        return WindowsShutdownStrategy()
    return PosixShutdownStrategy()


class LocalSupervisor:
    """Start and supervise the local worker entry-point process."""

    def __init__(
        self,
        *,
        worker_script: Path | str | None = None,
        popen_factory: PopenFactory = _default_popen,
        install_signals: bool = True,
        shutdown_strategy: ProcessShutdownStrategy | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._worker_script = (
            Path(worker_script).resolve()
            if worker_script is not None
            else Path(__file__).resolve().parents[3] / "worker.py"
        )
        self._popen_factory = popen_factory
        self._install_signals = install_signals
        self._shutdown_strategy = (
            shutdown_strategy
            if shutdown_strategy is not None
            else select_shutdown_strategy(platform_name=platform_name)
        )
        self._graceful_stop_sent = False

    def run(
        self,
        *,
        config: str | None = None,
        env_file: str | None = None,
        worker_once: bool = False,
        worker_max_jobs: int | None = None,
        worker_id: str | None = None,
    ) -> int:
        """Bootstrap run_local, start ``worker.py``, and propagate exit status."""
        runtime_context = bootstrap_runtime(
            "run_local",
            config_path=config,
            env_file=env_file,
        )
        settings: WorkerSettings = runtime_context.settings.worker
        absolute_config = None if config is None else str(Path(config).resolve())
        absolute_env = None if env_file is None else str(Path(env_file).resolve())
        if worker_id is not None:
            try:
                worker_id = normalize_uuid(worker_id)
            except RepositoryError as exc:
                raise WorkerError(f"invalid worker UUID: {worker_id}") from exc
        command = self._build_worker_command(
            config=absolute_config,
            env_file=absolute_env,
            worker_once=worker_once,
            worker_max_jobs=worker_max_jobs,
            worker_id=worker_id,
        )
        child = self._popen_factory(
            command,
            creationflags=self._shutdown_strategy.creationflags(),
        )
        stop_requested = threading.Event()
        originals = self._install_signal_handlers(stop_requested)
        try:
            while True:
                if stop_requested.is_set():
                    return self._shutdown_child(
                        child,
                        shutdown_grace_seconds=settings.shutdown_grace_seconds,
                    )
                try:
                    return child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            self._restore_signal_handlers(originals)

    def _build_worker_command(
        self,
        *,
        config: str | None,
        env_file: str | None,
        worker_once: bool,
        worker_max_jobs: int | None,
        worker_id: str | None,
    ) -> list[str]:
        command = [sys.executable, str(self._worker_script.resolve())]
        if config is not None:
            command.extend(["--config", config])
        if env_file is not None:
            command.extend(["--env-file", env_file])
        if worker_once:
            command.append("--once")
        if worker_max_jobs is not None:
            command.extend(["--max-jobs", str(worker_max_jobs)])
        if worker_id is not None:
            command.extend(["--worker-id", worker_id])
        return command

    def _install_signal_handlers(
        self,
        stop_requested: threading.Event,
    ) -> dict[int, SignalHandler]:
        if not self._install_signals or threading.current_thread() is not threading.main_thread():
            return {}

        originals: dict[int, SignalHandler] = {}

        def _handler(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            stop_requested.set()

        for signum_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, signum_name, None)
            if signum is None:
                continue
            originals[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        return originals

    @staticmethod
    def _restore_signal_handlers(originals: Mapping[int, SignalHandler]) -> None:
        for signum, handler in originals.items():
            signal.signal(signum, handler)

    def _shutdown_child(self, child: ChildProcess, *, shutdown_grace_seconds: float) -> int:
        grace = validate_positive_finite_number(
            shutdown_grace_seconds,
            field_name="shutdown_grace_seconds",
        )
        code = child.poll()
        if code is not None:
            return code
        if not self._graceful_stop_sent:
            self._shutdown_strategy.request_graceful_stop(child)
            self._graceful_stop_sent = True
        try:
            return child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            child.kill()
            return child.wait()


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the local supervisor argument parser."""
    parser = build_common_argument_parser("run_local", "Local startup coordinator.")
    parser.add_argument(
        "--worker-once",
        action="store_true",
        help="Forward --once to the worker child.",
    )
    parser.add_argument(
        "--worker-max-jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Forward --max-jobs N to the worker child.",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        metavar="UUID",
        help="Forward --worker-id UUID to the worker child.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local supervisor CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if (args.validate_config or args.database_status or args.migrate_database) and (
        args.worker_once or args.worker_max_jobs is not None or args.worker_id is not None
    ):
        parser.error("worker forwarding options cannot be combined with shared CLI modes")
    try:
        common_exit = handle_common_modes(args)
        if common_exit is not None:
            return common_exit
        return LocalSupervisor().run(
            config=args.config,
            env_file=args.env_file,
            worker_once=args.worker_once,
            worker_max_jobs=args.worker_max_jobs,
            worker_id=args.worker_id,
        )
    except (ConfigurationError, RuntimeBootstrapError, DatabaseError, WorkerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed

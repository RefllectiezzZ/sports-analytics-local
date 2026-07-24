"""Local worker process supervisor."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType
from typing import Protocol

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    RepositoryError,
    RuntimeBootstrapError,
    WorkerError,
)
from sports_analytics.core.runtime import validate_configuration
from sports_analytics.data.cli import migrate_database
from sports_analytics.data.types import normalize_uuid

SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


class ChildProcess(Protocol):
    """Subset of ``subprocess.Popen`` used by the supervisor."""

    def poll(self) -> int | None:
        """Return the child exit code if it has exited."""

    def wait(self, timeout: float | None = None) -> int:
        """Wait for child exit and return its exit code."""

    def terminate(self) -> None:
        """Request graceful child termination."""

    def kill(self) -> None:
        """Force child termination."""


class PopenFactory(Protocol):
    """Factory used to start the worker child process."""

    def __call__(self, command: Sequence[str]) -> ChildProcess:
        """Start ``command`` and return a child-process handle."""


def _default_popen(command: Sequence[str]) -> ChildProcess:
    return subprocess.Popen(list(command), shell=False)


class LocalSupervisor:
    """Start and supervise the local worker entry-point process."""

    def __init__(
        self,
        *,
        worker_script: Path | str | None = None,
        popen_factory: PopenFactory = _default_popen,
        install_signals: bool = True,
    ) -> None:
        self._worker_script = (
            Path(worker_script).resolve()
            if worker_script is not None
            else Path(__file__).resolve().parents[3] / "worker.py"
        )
        self._popen_factory = popen_factory
        self._install_signals = install_signals

    def run(
        self,
        *,
        config: str | None = None,
        env_file: str | None = None,
        worker_once: bool = False,
        worker_max_jobs: int | None = None,
        worker_id: str | None = None,
    ) -> int:
        """Ensure the database is ready, start ``worker.py``, and propagate exit status."""
        settings, paths = validate_configuration(config_path=config, env_file=env_file)
        migrate_database(settings, paths)
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
        child = self._popen_factory(command)
        stop_requested = threading.Event()
        originals = self._install_signal_handlers(child, stop_requested)
        try:
            while True:
                if stop_requested.is_set():
                    return self._terminate_child(
                        child,
                        shutdown_grace_seconds=settings.worker.shutdown_grace_seconds,
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
        child: ChildProcess,
        stop_requested: threading.Event,
    ) -> dict[int, SignalHandler]:
        if not self._install_signals or threading.current_thread() is not threading.main_thread():
            return {}

        originals: dict[int, SignalHandler] = {}

        def _handler(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            stop_requested.set()
            if child.poll() is None:
                child.terminate()

        for signum_name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, signum_name, None)
            if signum is None:
                continue
            originals[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        return originals

    @staticmethod
    def _restore_signal_handlers(originals: dict[int, SignalHandler]) -> None:
        for signum, handler in originals.items():
            signal.signal(signum, handler)

    @staticmethod
    def _terminate_child(child: ChildProcess, *, shutdown_grace_seconds: float) -> int:
        code = child.poll()
        if code is not None:
            return code
        child.terminate()
        try:
            return child.wait(timeout=max(0.0, float(shutdown_grace_seconds)))
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

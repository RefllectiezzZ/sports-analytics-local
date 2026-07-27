"""Deterministic local bookmaker acquisition scheduler (no network server).

Uses SQLite + JobRepository to enqueue autonomous sport-level acquisition jobs.
Does not start when ``bookmakers.enabled`` is false.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Final

from sports_analytics.bookmakers.scheduler_ops import (
    atomic_enqueue_autonomous_cycle,
    ensure_scheduler_anchor,
    resolve_next_scheduled_for,
)
from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    RepositoryError,
    RuntimeBootstrapError,
    SportsAnalyticsError,
)
from sports_analytics.core.runtime import bootstrap_runtime
from sports_analytics.core.settings import BookmakersSettings, Settings
from sports_analytics.jobs.errors import sanitize_error_text
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS

_LOGGER = logging.getLogger(__name__)
_SIGNAL_NAMES: Final[tuple[str, ...]] = ("SIGINT", "SIGTERM", "SIGBREAK")
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


class BookmakerScheduler:
    """Enqueue autonomous bookmaker acquisition jobs on a deterministic local cadence."""

    def __init__(
        self,
        *,
        settings: Settings,
        database_path: Path,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        poll_interval_seconds: float = 5.0,
        install_signals: bool = True,
    ) -> None:
        self._settings = settings
        self._bookmakers = settings.bookmakers
        self._database_path = database_path
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._poll_interval_seconds = poll_interval_seconds
        self._install_signals = install_signals
        self._stop_requested = threading.Event()

    def run_forever(self) -> int:
        """Run until a stop signal is received. Returns 0 on cooperative stop."""
        if not self._bookmakers.enabled:
            _LOGGER.info("bookmaker scheduler not started because bookmakers.enabled=false")
            return SUCCESS_EXIT

        originals: dict[int, SignalHandler] = {}
        try:
            originals = self._install_signal_handlers()
            _LOGGER.info(
                "bookmaker scheduler started preferred=%s comparison=%s",
                self._bookmakers.preferred_provider,
                self._bookmakers.comparison_provider,
            )
            while not self._stop_requested.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001 - keep scheduler alive
                    _LOGGER.error(
                        "bookmaker scheduler tick failed error=%s",
                        sanitize_error_text(exc),
                    )
                self._sleeper(self._poll_interval_seconds)
            return SUCCESS_EXIT
        finally:
            self._restore_signal_handlers(originals)

    def tick(self) -> int:
        """Evaluate due sport cycles once and enqueue missing jobs.

        Returns the number of newly enqueued jobs.
        """
        if not self._bookmakers.enabled:
            return 0
        now = self._normalize_now(self._clock())
        enqueued = 0
        for sport in SUPPORTED_BOOKMAKER_SPORTS:
            if self._enqueue_sport_if_due(sport=sport, now=now):
                enqueued += 1
        return enqueued

    def _enqueue_sport_if_due(self, *, sport: str, now: datetime) -> bool:
        try:
            anchor_state = ensure_scheduler_anchor(
                database_path=self._database_path,
                bookmakers=self._bookmakers,
                sport=sport,
                now=now,
            )
        except (ConfigurationError, RepositoryError, SportsAnalyticsError) as exc:
            _LOGGER.warning(
                "scheduler anchor skipped sport=%s error=%s",
                sport,
                sanitize_error_text(exc),
            )
            return False

        scheduled_for = resolve_next_scheduled_for(
            database_path=self._database_path,
            sport=sport,
            now=now,
            acquisition_interval_seconds=self._bookmakers.betano.acquisition_interval_seconds,
        )
        if scheduled_for > now:
            return False

        try:
            result = atomic_enqueue_autonomous_cycle(
                database_path=self._database_path,
                bookmakers=self._bookmakers,
                sport=sport,
                scheduled_for=scheduled_for,
                now=now,
            )
        except (ConfigurationError, RepositoryError, SportsAnalyticsError) as exc:
            _LOGGER.warning(
                "scheduler enqueue skipped sport=%s error=%s",
                sport,
                sanitize_error_text(exc),
            )
            return False

        if not result.inserted:
            _LOGGER.debug(
                "scheduler suppressed duplicate cycle sport=%s scheduled_for=%s",
                sport,
                scheduled_for.isoformat(),
            )
            return False
        _LOGGER.info(
            "scheduler enqueued sport=%s job_id=%s scheduled_for=%s anchor_created=%s",
            sport,
            result.job.id if result.job is not None else None,
            scheduled_for.isoformat(),
            anchor_state.anchor_created,
        )
        return True

    @staticmethod
    def _normalize_now(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def request_stop(self) -> None:
        """Request cooperative scheduler shutdown."""
        self._stop_requested.set()

    def _install_signal_handlers(self) -> dict[int, SignalHandler]:
        if not self._install_signals or threading.current_thread() is not threading.main_thread():
            return {}
        originals: dict[int, SignalHandler] = {}

        def _handler(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            self.request_stop()

        try:
            for signum_name in _SIGNAL_NAMES:
                signum = getattr(signal, signum_name, None)
                if signum is None:
                    continue
                previous = signal.getsignal(signum)
                signal.signal(signum, _handler)
                originals[signum] = previous
        except BaseException:
            for signum, previous in list(originals.items()):
                try:
                    signal.signal(signum, previous)
                except Exception:  # noqa: BLE001
                    pass
            raise
        return originals

    def _restore_signal_handlers(self, originals: dict[int, SignalHandler]) -> None:
        for signum, handler in originals.items():
            try:
                signal.signal(signum, handler)
            except Exception:  # noqa: BLE001
                pass


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the bookmaker scheduler CLI parser."""
    return build_common_argument_parser(
        "bookmaker-scheduler",
        "Local bookmaker acquisition scheduler (SQLite enqueue only).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bookmaker scheduler process."""
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        common_exit = handle_common_modes(args)
        if common_exit is not None:
            return common_exit
        runtime = bootstrap_runtime(
            "bookmaker-scheduler",
            config_path=args.config,
            env_file=args.env_file,
        )
        bookmakers: BookmakersSettings = runtime.settings.bookmakers
        if not bookmakers.enabled:
            print("bookmakers.enabled=false; scheduler exiting without work", file=sys.stderr)
            return SUCCESS_EXIT
        scheduler = BookmakerScheduler(
            settings=runtime.settings,
            database_path=runtime.database_path,
        )
        return scheduler.run_forever()
    except (ConfigurationError, RuntimeBootstrapError, DatabaseError, SportsAnalyticsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic local bookmaker acquisition scheduler (no network server).

Uses SQLite + JobRepository to enqueue acquisition jobs on provider-specific
intervals. Does not start when ``bookmakers.enabled`` is false.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Final

from sports_analytics.bookmakers.enqueue import enqueue_bookmaker_acquisition
from sports_analytics.bookmakers.types import (
    DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
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
from sports_analytics.core.settings import BookmakerProviderSettings, BookmakersSettings, Settings
from sports_analytics.data.codec import parse_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.jobs.errors import sanitize_error_text
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS

_LOGGER = logging.getLogger(__name__)
_SIGNAL_NAMES: Final[tuple[str, ...]] = ("SIGINT", "SIGTERM", "SIGBREAK")
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


class BookmakerScheduler:
    """Enqueue bookmaker acquisition jobs on a deterministic local cadence."""

    def __init__(
        self,
        *,
        settings: Settings,
        database_path,
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
        """Evaluate due provider/sport cycles once and enqueue missing jobs.

        Returns the number of newly enqueued jobs.
        """
        if not self._bookmakers.enabled:
            return 0
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        enqueued = 0
        for provider_id, provider_settings in self._enabled_providers():
            for sport in SUPPORTED_BOOKMAKER_SPORTS:
                if self._enqueue_if_due(
                    provider_id=provider_id,
                    sport=sport,
                    provider_settings=provider_settings,
                    now=now,
                ):
                    enqueued += 1
        return enqueued

    def _enabled_providers(self) -> list[tuple[str, BookmakerProviderSettings]]:
        providers: list[tuple[str, BookmakerProviderSettings]] = []
        if self._bookmakers.betano.enabled:
            providers.append((PROVIDER_BETANO_PT, self._bookmakers.betano))
        if self._bookmakers.betclic.enabled:
            providers.append((PROVIDER_BETCLIC_PT, self._bookmakers.betclic))
        return providers

    def _enqueue_if_due(
        self,
        *,
        provider_id: str,
        sport: str,
        provider_settings: BookmakerProviderSettings,
        now: datetime,
    ) -> bool:
        scheduled_for = self._next_scheduled_for(
            provider_id=provider_id,
            sport=sport,
            provider_settings=provider_settings,
            now=now,
        )
        if scheduled_for > now:
            return False
        # Align to the interval boundary so restarts recover deterministically.
        scheduled_for = self._align_scheduled_for(
            provider_settings=provider_settings,
            candidate=scheduled_for,
            now=now,
        )
        idempotency_key = (
            f"bookmaker-acq:{provider_id}:{sport}:{scheduled_for.strftime('%Y%m%dT%H%M%SZ')}"
        )
        try:
            job = enqueue_bookmaker_acquisition(
                database_path=self._database_path,
                bookmakers=self._bookmakers,
                provider_id=provider_id,
                sport=sport,
                acquisition_cycle_id=idempotency_key.replace(":", "-"),
                maximum_attempts=DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
                actor="bookmaker-scheduler",
                created_at=now,
                idempotency_key=idempotency_key,
            )
        except (ConfigurationError, RepositoryError, SportsAnalyticsError) as exc:
            _LOGGER.warning(
                "scheduler enqueue skipped provider=%s sport=%s error=%s",
                provider_id,
                sport,
                sanitize_error_text(exc),
            )
            return False

        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                _cycle_id, inserted = repo.insert_scheduler_cycle(
                    provider_id=provider_id,
                    sport=sport,
                    scheduled_for=scheduled_for,
                    enqueued_at=now,
                    job_id=job.id,
                    suppressed_duplicate=False,
                )
        if not inserted:
            _LOGGER.debug(
                "scheduler suppressed duplicate cycle provider=%s sport=%s scheduled_for=%s",
                provider_id,
                sport,
                scheduled_for.isoformat(),
            )
            return False
        _LOGGER.info(
            "scheduler enqueued provider=%s sport=%s job_id=%s scheduled_for=%s",
            provider_id,
            sport,
            job.id,
            scheduled_for.isoformat(),
        )
        return True

    def _next_scheduled_for(
        self,
        *,
        provider_id: str,
        sport: str,
        provider_settings: BookmakerProviderSettings,
        now: datetime,
    ) -> datetime:
        with connect_database(self._database_path, read_only=True) as connection:
            repo = BookmakerRepository(connection)
            latest = repo.latest_scheduler_cycle(provider_id=provider_id, sport=sport)
            status = repo.get_provider_status(provider_id)

        if latest is None:
            # First cycle after process start: honor initial delay.
            return now + timedelta(seconds=provider_settings.initial_delay_seconds)

        last_scheduled = parse_utc_timestamp(str(latest["scheduled_for_utc"]))
        next_from_interval = last_scheduled + timedelta(
            seconds=provider_settings.acquisition_interval_seconds
        )

        next_eligible = now
        if status is not None:
            next_eligible_raw = status.get("next_eligible_at_utc")
            if isinstance(next_eligible_raw, str) and next_eligible_raw:
                next_eligible = parse_utc_timestamp(next_eligible_raw)
            if status.get("status") == "blocked":
                # Blocked providers wait for cooldown rather than aggressive retry.
                return max(next_from_interval, next_eligible)

        return max(next_from_interval, next_eligible)

    @staticmethod
    def _align_scheduled_for(
        *,
        provider_settings: BookmakerProviderSettings,
        candidate: datetime,
        now: datetime,
    ) -> datetime:
        """Clamp a due candidate so concurrent ticks do not invent new slots."""
        del provider_settings
        if candidate > now:
            return candidate
        return candidate

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

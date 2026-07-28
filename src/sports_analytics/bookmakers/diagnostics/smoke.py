"""End-to-end provider smoke tests using verified extraction profiles."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sports_analytics.bookmakers.diagnostics.paths import resolve_diagnostic_directory
from sports_analytics.bookmakers.diagnostics.persistence import publish_diagnostic_json
from sports_analytics.bookmakers.diagnostics.redaction import redact_structure
from sports_analytics.bookmakers.loader import load_verified_bookmaker_quotes
from sports_analytics.bookmakers.markets import DEFINITION_FOOTBALL_MATCH_RESULT_1X2
from sports_analytics.bookmakers.service import BookmakerIngestionService
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    BookmakerIngestionResult,
)
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.contracts import BrowserBlockReason
from sports_analytics.sources.browser.playwright_runtime import PlaywrightBrowserSession

_MIN_EVENTS = 2
_MIN_MARKETS_PER_EVENT = 1
_REQUIRED_CYCLES = 2
_SAFE_INGESTION_STATUSES = frozenset(
    {"blocked", "drift-detected", "failed", "partial", "succeeded", "unavailable"}
)
_SAFE_FAILURE_STAGES = frozenset(
    {
        "acquisition-failure",
        "blocked-acquisition",
        "no-provider-response",
        "no-recognized-profile-response",
        "parsed-events-zero-supported-quotes",
        "recognized-response-zero-events",
        "snapshot-admission-failure",
    }
)
_SAFE_BLOCK_REASONS = frozenset(reason.value for reason in BrowserBlockReason)


@dataclass(frozen=True, slots=True)
class SmokeCycleResult:
    """Outcome of one complete smoke-test acquisition cycle."""

    cycle_number: int
    acquisition_cycle_id: str
    events_extracted: int
    supported_market_count: int
    valid_quote_count: int
    snapshot_id: str | None
    snapshot_checksum: str | None
    snapshot_verified: bool
    snapshot_reused: bool
    observed_at_utc: str | None
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class SmokeFailureTelemetry:
    """Fixed, bounded failure-stage evidence with no provider-controlled text."""

    ingestion_status: str
    response_observation_count: int
    recognized_profile_response_count: int
    events_observed: int
    valid_quotes_observed: int
    snapshot_created: bool
    blocked: bool
    block_reason: str | None
    failure_stage: str

    def __post_init__(self) -> None:
        if self.ingestion_status not in _SAFE_INGESTION_STATUSES:
            raise ConfigurationError("smoke ingestion status is not allowlisted")
        if self.failure_stage not in _SAFE_FAILURE_STAGES:
            raise ConfigurationError("smoke failure stage is not allowlisted")
        if self.block_reason is not None and self.block_reason not in _SAFE_BLOCK_REASONS:
            raise ConfigurationError("smoke block reason is not allowlisted")
        counts = (
            self.response_observation_count,
            self.recognized_profile_response_count,
            self.events_observed,
            self.valid_quotes_observed,
        )
        if any(value < 0 or value > 1_000_000 for value in counts):
            raise ConfigurationError("smoke telemetry count is outside the fixed bound")
        if self.recognized_profile_response_count > self.response_observation_count:
            raise ConfigurationError("recognized response count exceeds observed responses")
        if self.blocked != (self.ingestion_status == "blocked"):
            raise ConfigurationError("smoke blocked telemetry contradicts ingestion status")
        if not self.blocked and self.block_reason is not None:
            raise ConfigurationError("smoke block reason requires blocked ingestion")


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """Outcome of a bounded provider smoke test."""

    provider: str
    sport: str
    succeeded: bool
    failure_reason: str | None
    profile_id: str | None
    profile_verified: bool
    cycles: tuple[SmokeCycleResult, ...]
    diagnostic_relative_path: str
    acceptance_summary: dict[str, Any]
    failure_telemetry: SmokeFailureTelemetry | None = None


def smoke_bookmaker(
    *,
    provider_id: str,
    sport: str,
    duration_seconds: int = 60,
    diagnostic_directory: str | Path | None = None,
    database_path: Path | None = None,
    raw_directory: Path | None = None,
    snapshots_directory: Path | None = None,
    bookmakers: BookmakersSettings | None = None,
    session: PlaywrightBrowserSession | None = None,
    extraction_profile: Any | None = None,
    clock: Callable[[], datetime] | None = None,
    service: BookmakerIngestionService | None = None,
) -> SmokeResult:
    """Run two complete acquisition cycles through the production ingestion path."""
    if provider_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
        msg = f"unsupported smoke provider: {provider_id}"
        raise ConfigurationError(msg)
    if sport not in SUPPORTED_BOOKMAKER_SPORTS:
        msg = f"unsupported smoke sport: {sport}"
        raise ConfigurationError(msg)
    if duration_seconds < 1 or duration_seconds > 600:
        msg = "duration_seconds must be between 1 and 600"
        raise ConfigurationError(msg)
    profile = extraction_profile or get_verified_extraction_profile(provider_id)
    profile_id = None if profile is None else profile.profile_id
    profile_verified = False if profile is None else bool(profile.verified)
    if profile is None or not profile.verified:
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason="no-verified-extraction-profile",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
        )

    base = Path.cwd()
    db_path = database_path or base / "storage" / "operational.sqlite3"
    raw_dir = raw_directory or base / "storage" / "raw"
    snap_dir = snapshots_directory or base / "storage" / "snapshots"
    ensure_database_ready(db_path)
    raw_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)
    clock_fn = clock if clock is not None else (lambda: datetime.now(tz=UTC))
    deadline = clock_fn() + timedelta(seconds=duration_seconds)
    settings = bookmakers
    if settings is None:
        settings = BookmakersSettings(enabled=True)
    base_session = session or PlaywrightBrowserSession(clock=clock_fn)
    browser_session = _DeadlineBrowserSession(base_session, deadline_at_utc=deadline)
    ingestion = service or BookmakerIngestionService(
        database_path=db_path,
        raw_directory=raw_dir,
        snapshots_directory=snap_dir,
        bookmakers=settings,
        clock=clock_fn,
        session=browser_session,
    )

    cycles: list[SmokeCycleResult] = []
    for cycle_number in range(1, _REQUIRED_CYCLES + 1):
        if clock_fn() > deadline:
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="duration-deadline-exceeded",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
            )
        cycle_started = time.monotonic()
        cycle_id = f"smoke-{provider_id}-{sport}-{cycle_number}"
        try:
            result = ingestion.ingest(
                provider_id=provider_id,
                sport=sport,
                observed_at_utc=clock_fn(),
                acquisition_cycle_id=cycle_id,
                actor="smoke-bookmaker",
                attempt_number=1,
                maximum_attempts=2,
            )
        except Exception:  # noqa: BLE001 - convert to fixed smoke failure
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="acquisition-failed",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
                telemetry=None,
            )
        elapsed = time.monotonic() - cycle_started
        if result.status == "blocked":
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="blocked-acquisition",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
                telemetry=_failure_telemetry_from_ingestion(result),
            )
        if result.snapshot_id is None or result.valid_quotes_observed < 1:
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="zero-events-or-odds",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
                telemetry=_failure_telemetry_from_ingestion(result),
            )
        try:
            with connect_database(db_path, read_only=True) as connection:
                loaded = load_verified_bookmaker_quotes(
                    database_connection=connection,
                    snapshots_directory=snap_dir,
                    raw_directory=raw_dir,
                    snapshot_id=result.snapshot_id,
                )
        except Exception:  # noqa: BLE001 - convert to fixed smoke failure
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="snapshot-verification-failed",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
                telemetry=_failure_telemetry_from_ingestion(
                    result,
                    stage_override="snapshot-admission-failure",
                ),
            )
        if (
            not loaded.verified
            or loaded.catalogue is None
            or loaded.quote_count < 1
            or loaded.event_count < _MIN_EVENTS
        ):
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="snapshot-verification-failed",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
                telemetry=_failure_telemetry_from_ingestion(
                    result,
                    stage_override="snapshot-admission-failure",
                ),
            )
        event_ids_with_quotes = {
            quote.identity.canonical_event_id
            for _, quote in loaded.verified_quotes_by_observation_id
        }
        if len(event_ids_with_quotes) < loaded.event_count:
            return _failed_smoke(
                provider_id=provider_id,
                sport=sport,
                reason="per-event-quote-coverage-failed",
                profile_id=profile_id,
                profile_verified=profile_verified,
                diagnostic_directory=diagnostic_directory,
                cycles=tuple(cycles),
                telemetry=_failure_telemetry_from_ingestion(
                    result,
                    stage_override="snapshot-admission-failure",
                ),
            )
        cycles.append(
            SmokeCycleResult(
                cycle_number=cycle_number,
                acquisition_cycle_id=cycle_id,
                events_extracted=loaded.event_count,
                supported_market_count=max(loaded.quote_count, result.valid_quotes_observed),
                valid_quote_count=loaded.quote_count,
                snapshot_id=loaded.snapshot_id,
                snapshot_checksum=loaded.checksum_sha256,
                snapshot_verified=True,
                snapshot_reused=bool(result.snapshot_reused),
                observed_at_utc=result.observed_at_utc or format_utc_timestamp(clock_fn()),
                duration_seconds=elapsed,
            )
        )

    if len(cycles) != _REQUIRED_CYCLES:
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason="second-cycle-failed",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
            cycles=tuple(cycles),
        )
    if any(
        cycle.snapshot_id is None or not cycle.snapshot_verified or cycle.valid_quote_count < 1
        for cycle in cycles
    ):
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason="incomplete-cycle-evidence",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
            cycles=tuple(cycles),
        )
    refresh_or_reuse = _second_cycle_refresh_or_reuse_proven(tuple(cycles))
    if refresh_or_reuse is None:
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason="second-cycle-refresh-or-reuse-unproven",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
            cycles=tuple(cycles),
        )

    summary = {
        "provider": provider_id,
        "sport": sport,
        "cycles": len(cycles),
        "events_extracted": cycles[0].events_extracted,
        "valid_quote_count": cycles[0].valid_quote_count,
        "target_market": DEFINITION_FOOTBALL_MATCH_RESULT_1X2 if sport == "football" else None,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
        "second_cycle_reused_or_refreshed": True,
        "second_cycle_proof": refresh_or_reuse,
    }
    artifact = _write_smoke_artifact(
        provider_id=provider_id,
        sport=sport,
        succeeded=True,
        failure_reason=None,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=tuple(cycles),
        acceptance_summary=summary,
        diagnostic_directory=diagnostic_directory,
    )
    return SmokeResult(
        provider=provider_id,
        sport=sport,
        succeeded=True,
        failure_reason=None,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=tuple(cycles),
        diagnostic_relative_path=artifact,
        acceptance_summary=redact_structure(summary),
    )


def evaluate_fake_session_smoke(
    *,
    provider_id: str,
    sport: str,
    events_extracted: int,
    markets_with_odds: int,
    profile_verified: bool,
    profile_id: str | None,
    valid_quote_count: int | None = None,
    snapshot_verified: bool = True,
    snapshot_reused_second_cycle: bool | None = True,
) -> SmokeResult:
    """Evaluate smoke criteria from injected test evidence without live browser."""
    quotes = markets_with_odds if valid_quote_count is None else valid_quote_count
    succeeded = (
        profile_verified
        and events_extracted >= _MIN_EVENTS
        and markets_with_odds >= _MIN_MARKETS_PER_EVENT * events_extracted
        and quotes >= events_extracted
        and snapshot_verified
    )
    failure = None if succeeded else "zero-events-or-odds"
    cycles: tuple[SmokeCycleResult, ...] = ()
    if succeeded:
        cycles = (
            SmokeCycleResult(
                cycle_number=1,
                acquisition_cycle_id=f"smoke-{provider_id}-{sport}-1",
                events_extracted=events_extracted,
                supported_market_count=markets_with_odds,
                valid_quote_count=quotes,
                snapshot_id="snap-test-1",
                snapshot_checksum="a" * 64,
                snapshot_verified=True,
                snapshot_reused=False,
                observed_at_utc="2026-07-26T12:00:00Z",
                duration_seconds=1.0,
            ),
            SmokeCycleResult(
                cycle_number=2,
                acquisition_cycle_id=f"smoke-{provider_id}-{sport}-2",
                events_extracted=events_extracted,
                supported_market_count=markets_with_odds,
                valid_quote_count=quotes,
                snapshot_id=(
                    "snap-test-2" if snapshot_reused_second_cycle is False else "snap-test-1"
                ),
                snapshot_checksum=("b" * 64 if snapshot_reused_second_cycle is False else "a" * 64),
                snapshot_verified=True,
                snapshot_reused=snapshot_reused_second_cycle is True,
                observed_at_utc="2026-07-26T12:01:00Z",
                duration_seconds=1.0,
            ),
        )
        refresh_or_reuse = _second_cycle_refresh_or_reuse_proven(cycles)
        if refresh_or_reuse is None:
            succeeded = False
            failure = "second-cycle-refresh-or-reuse-unproven"
            cycles = ()
    else:
        refresh_or_reuse = None
    summary = {
        "provider": provider_id,
        "sport": sport,
        "events_extracted": events_extracted,
        "markets_with_odds": markets_with_odds,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
        "second_cycle_reused_or_refreshed": succeeded,
        "second_cycle_proof": refresh_or_reuse,
    }
    return SmokeResult(
        provider=provider_id,
        sport=sport,
        succeeded=succeeded,
        failure_reason=failure,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=cycles,
        diagnostic_relative_path="smoke-fake-session.json",
        acceptance_summary=redact_structure(summary),
        failure_telemetry=None,
    )


def _failure_telemetry_from_ingestion(
    result: BookmakerIngestionResult,
    *,
    stage_override: str | None = None,
) -> SmokeFailureTelemetry:
    status = result.status if result.status in _SAFE_INGESTION_STATUSES else "failed"
    block_reason = result.block_reason if result.block_reason in _SAFE_BLOCK_REASONS else None
    if stage_override is not None:
        stage = stage_override
    elif status == "blocked":
        stage = "blocked-acquisition"
    elif result.response_observation_count == 0:
        stage = "no-provider-response"
    elif result.recognized_profile_response_count == 0:
        stage = "no-recognized-profile-response"
    elif result.events_observed == 0:
        stage = "recognized-response-zero-events"
    elif result.valid_quotes_observed == 0:
        stage = "parsed-events-zero-supported-quotes"
    else:
        stage = "snapshot-admission-failure"
    return SmokeFailureTelemetry(
        ingestion_status=status,
        response_observation_count=result.response_observation_count,
        recognized_profile_response_count=result.recognized_profile_response_count,
        events_observed=result.events_observed,
        valid_quotes_observed=result.valid_quotes_observed,
        snapshot_created=result.snapshot_id is not None,
        blocked=status == "blocked",
        block_reason=block_reason,
        failure_stage=stage,
    )


def _failed_smoke(
    *,
    provider_id: str,
    sport: str,
    reason: str,
    profile_id: str | None,
    profile_verified: bool,
    diagnostic_directory: str | Path | None,
    cycles: tuple[SmokeCycleResult, ...] = (),
    telemetry: SmokeFailureTelemetry | None = None,
) -> SmokeResult:
    summary = {
        "provider": provider_id,
        "sport": sport,
        "failure_reason": reason,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
        "failure_telemetry": None if telemetry is None else asdict(telemetry),
    }
    artifact = _write_smoke_artifact(
        provider_id=provider_id,
        sport=sport,
        succeeded=False,
        failure_reason=reason,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=cycles,
        acceptance_summary=summary,
        failure_telemetry=telemetry,
        diagnostic_directory=diagnostic_directory,
    )
    return SmokeResult(
        provider=provider_id,
        sport=sport,
        succeeded=False,
        failure_reason=reason,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=cycles,
        diagnostic_relative_path=artifact,
        acceptance_summary=redact_structure(summary),
        failure_telemetry=telemetry,
    )


def _write_smoke_artifact(
    *,
    provider_id: str,
    sport: str,
    succeeded: bool,
    failure_reason: str | None,
    profile_id: str | None,
    profile_verified: bool,
    cycles: tuple[SmokeCycleResult, ...],
    acceptance_summary: dict[str, Any],
    failure_telemetry: SmokeFailureTelemetry | None = None,
    diagnostic_directory: str | Path | None,
) -> str:
    output_dir = resolve_diagnostic_directory(diagnostic_directory)
    filename = f"smoke-{provider_id}-{sport}.json"
    path = output_dir / filename
    payload = {
        "provider": provider_id,
        "sport": sport,
        "succeeded": succeeded,
        "failure_reason": failure_reason,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
        "cycles": [asdict(item) for item in cycles],
        "acceptance_summary": redact_structure(acceptance_summary),
        "failure_telemetry": (None if failure_telemetry is None else asdict(failure_telemetry)),
    }
    publish_diagnostic_json(path, payload)
    return filename


def _second_cycle_refresh_or_reuse_proven(cycles: tuple[SmokeCycleResult, ...]) -> str | None:
    """Return proof label when cycle 2 is a real refresh or deterministic reuse."""
    if len(cycles) != 2:
        return None
    first, second = cycles
    if (
        first.observed_at_utc is None
        or second.observed_at_utc is None
        or first.snapshot_id is None
        or second.snapshot_id is None
        or first.snapshot_checksum is None
        or second.snapshot_checksum is None
    ):
        return None
    if second.observed_at_utc > first.observed_at_utc and second.snapshot_verified:
        if (
            second.snapshot_id != first.snapshot_id
            or second.snapshot_checksum != first.snapshot_checksum
        ):
            return "refresh"
    if (
        second.snapshot_reused
        and second.snapshot_id == first.snapshot_id
        and second.snapshot_checksum == first.snapshot_checksum
        and second.snapshot_verified
        and second.observed_at_utc >= first.observed_at_utc
    ):
        return "deterministic-reuse"
    return None


class _DeadlineBrowserSession:
    """Inject an acquisition deadline into every browser acquire call."""

    def __init__(self, inner: Any, *, deadline_at_utc: datetime) -> None:
        self._inner = inner
        self._deadline_at_utc = deadline_at_utc

    def acquire(self, **kwargs: Any) -> Any:
        kwargs.setdefault("deadline_at_utc", self._deadline_at_utc)
        return self._inner.acquire(**kwargs)

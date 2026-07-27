"""Atomic scheduler enqueue operations (single transaction)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sports_analytics.bookmakers.types import (
    BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER,
    DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
    INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
)
from sports_analytics.core.exceptions import (
    ConfigurationError,
    RepositoryError,
    SportsAnalyticsError,
)
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import (
    DEFAULT_JOB_PRIORITY,
    JobRecord,
    JsonValue,
    validate_identifier,
)
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS


@dataclass(frozen=True, slots=True)
class SchedulerAnchorState:
    """Resolved scheduler anchor for one sport."""

    provider_id: str
    sport: str
    first_due_at: datetime
    anchor_created: bool


@dataclass(frozen=True, slots=True)
class SchedulerEnqueueResult:
    """Outcome of one atomic scheduler enqueue attempt."""

    job: JobRecord | None
    cycle_id: str | None
    inserted: bool
    scheduled_for: datetime | None


@dataclass(frozen=True, slots=True)
class AutonomousSchedulingPolicy:
    """Derived autonomous scheduler cadence from enabled bookmaker providers."""

    enabled_provider_ids: tuple[str, ...]
    initial_delay_seconds: int
    acquisition_interval_seconds: int


def resolve_autonomous_scheduling_policy(
    bookmakers: BookmakersSettings,
) -> AutonomousSchedulingPolicy | None:
    """Derive scheduler timing from the enabled provider set."""
    enabled: list[str] = []
    if bookmakers.betano.enabled:
        enabled.append(PROVIDER_BETANO_PT)
    if bookmakers.betclic.enabled:
        enabled.append(PROVIDER_BETCLIC_PT)
    if not enabled:
        return None
    if enabled == [PROVIDER_BETANO_PT]:
        settings = bookmakers.betano
    elif enabled == [PROVIDER_BETCLIC_PT]:
        settings = bookmakers.betclic
    else:
        settings = bookmakers.betano
        interval = min(
            bookmakers.betano.acquisition_interval_seconds,
            bookmakers.betclic.acquisition_interval_seconds,
        )
        return AutonomousSchedulingPolicy(
            enabled_provider_ids=tuple(enabled),
            initial_delay_seconds=bookmakers.betano.initial_delay_seconds,
            acquisition_interval_seconds=interval,
        )
    return AutonomousSchedulingPolicy(
        enabled_provider_ids=tuple(enabled),
        initial_delay_seconds=settings.initial_delay_seconds,
        acquisition_interval_seconds=settings.acquisition_interval_seconds,
    )


def ensure_scheduler_anchor(
    *,
    database_path: Path,
    bookmakers: BookmakersSettings,
    sport: str,
    now: datetime,
) -> SchedulerAnchorState:
    """Create or read the persisted first-cycle anchor without enqueueing."""
    sport_code = validate_identifier(sport, field_name="sport")
    if sport_code not in SUPPORTED_BOOKMAKER_SPORTS:
        msg = f"unsupported bookmaker sport: {sport_code}"
        raise ConfigurationError(msg)
    provider_id = BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER
    policy = resolve_autonomous_scheduling_policy(bookmakers)
    if policy is None:
        msg = "no bookmaker providers are enabled for autonomous scheduling"
        raise ConfigurationError(msg)
    initial_delay = policy.initial_delay_seconds
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            repo = BookmakerRepository(connection)
            anchor = repo.get_scheduler_anchor(provider_id=provider_id, sport=sport_code)
            latest = repo.latest_scheduler_cycle(provider_id=provider_id, sport=sport_code)
            if anchor is not None:
                first_due = parse_utc_timestamp(str(anchor["first_due_at_utc"]))
                return SchedulerAnchorState(
                    provider_id=provider_id,
                    sport=sport_code,
                    first_due_at=first_due,
                    anchor_created=False,
                )
            if latest is not None:
                first_due = parse_utc_timestamp(str(latest["scheduled_for_utc"]))
                repo.upsert_scheduler_anchor(
                    provider_id=provider_id,
                    sport=sport_code,
                    first_due_at=first_due,
                    anchor_set_at=now,
                )
                return SchedulerAnchorState(
                    provider_id=provider_id,
                    sport=sport_code,
                    first_due_at=first_due,
                    anchor_created=True,
                )
            first_due = now + timedelta(seconds=initial_delay)
            repo.upsert_scheduler_anchor(
                provider_id=provider_id,
                sport=sport_code,
                first_due_at=first_due,
                anchor_set_at=now,
            )
            return SchedulerAnchorState(
                provider_id=provider_id,
                sport=sport_code,
                first_due_at=first_due,
                anchor_created=True,
            )


def resolve_next_scheduled_for(
    *,
    database_path: Path,
    sport: str,
    now: datetime,
    bookmakers: BookmakersSettings,
) -> datetime:
    """Compute the next due slot from anchor/cycle history."""
    sport_code = validate_identifier(sport, field_name="sport")
    policy = resolve_autonomous_scheduling_policy(bookmakers)
    if policy is None:
        return now
    acquisition_interval_seconds = policy.acquisition_interval_seconds
    provider_id = BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER
    with connect_database(database_path, read_only=True) as connection:
        repo = BookmakerRepository(connection)
        latest = repo.latest_scheduler_cycle(provider_id=provider_id, sport=sport_code)
        anchor = repo.get_scheduler_anchor(provider_id=provider_id, sport=sport_code)
        provider_statuses = [
            repo.get_provider_status(enabled_provider, sport_code)
            for enabled_provider in policy.enabled_provider_ids
        ]
    if latest is None:
        if anchor is not None:
            return parse_utc_timestamp(str(anchor["first_due_at_utc"]))
        return now
    last_scheduled = parse_utc_timestamp(str(latest["scheduled_for_utc"]))
    next_from_interval = last_scheduled + timedelta(seconds=acquisition_interval_seconds)
    next_eligible = now
    for status in provider_statuses:
        if status is None:
            continue
        if status.get("status") != "blocked":
            continue
        next_eligible_raw = status.get("next_eligible_at_utc")
        if isinstance(next_eligible_raw, str) and next_eligible_raw:
            next_eligible = max(next_eligible, parse_utc_timestamp(next_eligible_raw))
    return max(next_from_interval, next_eligible)


def atomic_enqueue_autonomous_cycle(
    *,
    database_path: Path,
    bookmakers: BookmakersSettings,
    sport: str,
    scheduled_for: datetime,
    now: datetime,
    actor: str = "bookmaker-scheduler",
    maximum_attempts: int = DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
) -> SchedulerEnqueueResult:
    """Atomically create job and scheduler cycle in one transaction."""
    if not bookmakers.enabled:
        msg = "bookmakers.enabled must be true to enqueue bookmaker acquisition"
        raise ConfigurationError(msg)
    policy = resolve_autonomous_scheduling_policy(bookmakers)
    if policy is None:
        msg = "no bookmaker providers are enabled for autonomous scheduling"
        raise ConfigurationError(msg)
    sport_code = validate_identifier(sport, field_name="sport")
    provider_id = BOOKMAKER_AUTONOMOUS_SCHEDULER_PROVIDER
    scheduled = scheduled_for
    idempotency_key = f"bookmaker-auto:{sport_code}:{scheduled.strftime('%Y%m%dT%H%M%SZ')}"
    acquisition_cycle_id = idempotency_key.replace(":", "-")
    payload: dict[str, JsonValue] = {
        "sport": sport_code,
        "acquisition_cycle_id": acquisition_cycle_id,
        "observed_at_utc": None,
    }
    try:
        with connect_database(database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                existing = connection.execute(
                    """
                    SELECT id, job_id FROM bookmaker_scheduler_cycles
                    WHERE provider_id = ? AND sport = ? AND scheduled_for = ?
                    """,
                    (
                        provider_id,
                        sport_code,
                        format_utc_timestamp(scheduled),
                    ),
                ).fetchone()
                if existing is not None:
                    return SchedulerEnqueueResult(
                        job=None,
                        cycle_id=str(existing["id"]),
                        inserted=False,
                        scheduled_for=scheduled,
                    )
                jobs = JobRepository(connection)
                job = jobs.create_job(
                    job_type=INGEST_BOOKMAKER_AUTONOMOUS_CYCLE_JOB_TYPE,
                    payload=payload,
                    maximum_attempts=maximum_attempts,
                    actor=actor,
                    created_at=now,
                    idempotency_key=idempotency_key,
                    priority=DEFAULT_JOB_PRIORITY,
                )
                cycle_id, inserted = repo.insert_scheduler_cycle(
                    provider_id=provider_id,
                    sport=sport_code,
                    scheduled_for=scheduled,
                    enqueued_at=now,
                    job_id=job.id,
                    suppressed_duplicate=False,
                )
                return SchedulerEnqueueResult(
                    job=job,
                    cycle_id=cycle_id,
                    inserted=inserted,
                    scheduled_for=scheduled,
                )
    except (RepositoryError, SportsAnalyticsError) as exc:
        raise ConfigurationError(str(exc)) from exc

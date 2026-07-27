"""Autonomous scheduler policy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.bookmakers.scheduler_ops import (
    resolve_autonomous_scheduling_policy,
    resolve_next_scheduled_for,
)
from sports_analytics.core.settings import BookmakerProviderSettings, BookmakersSettings
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.repositories.jobs import JobRepository

T0 = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_betclic_only_uses_betclic_intervals() -> None:
    settings = BookmakersSettings(
        enabled=True,
        betano=BookmakerProviderSettings(enabled=False, initial_delay_seconds=30),
        betclic=BookmakerProviderSettings(
            enabled=True,
            initial_delay_seconds=45,
            acquisition_interval_seconds=600,
        ),
    )
    policy = resolve_autonomous_scheduling_policy(settings)
    assert policy is not None
    assert policy.initial_delay_seconds == 45
    assert policy.acquisition_interval_seconds == 600


def test_no_enabled_providers_returns_none_policy() -> None:
    settings = BookmakersSettings(
        enabled=True,
        betano=BookmakerProviderSettings(enabled=False),
        betclic=BookmakerProviderSettings(enabled=False),
    )
    assert resolve_autonomous_scheduling_policy(settings) is None


def test_blocked_cooldown_uses_provider_status_not_scheduler_provider(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    settings = BookmakersSettings(enabled=True)
    blocked_until = T0 + timedelta(minutes=30)
    with connect_database(database) as connection:
        with transaction(connection, immediate=True):
            repo = BookmakerRepository(connection)
            jobs = JobRepository(connection)
            job = jobs.create_job(
                job_type="ingest.bookmaker-autonomous-cycle",
                payload={"sport": "football", "acquisition_cycle_id": "x", "observed_at_utc": None},
                maximum_attempts=2,
                actor="test",
                created_at=T0,
                idempotency_key="bookmaker-auto:football:blocked-test",
            )
            repo.upsert_scheduler_anchor(
                provider_id="bookmaker-autonomous",
                sport="football",
                first_due_at=T0,
                anchor_set_at=T0,
            )
            repo.insert_scheduler_cycle(
                provider_id="bookmaker-autonomous",
                sport="football",
                scheduled_for=T0,
                enqueued_at=T0,
                job_id=job.id,
            )
            repo.upsert_provider_status(
                provider_id="betano-pt",
                sport="football",
                status="blocked",
                updated_at=T0,
                next_eligible_at=blocked_until,
                last_valid_snapshot_id=None,
                events_observed=0,
                valid_quotes_observed=0,
            )
    scheduled = resolve_next_scheduled_for(
        database_path=database,
        sport="football",
        now=T0,
        bookmakers=settings,
    )
    assert scheduled >= blocked_until

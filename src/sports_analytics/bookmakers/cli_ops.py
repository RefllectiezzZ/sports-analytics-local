"""Bookmaker CLI operations for scrape coordinator integration."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sports_analytics.bookmakers.diagnostics.probe import probe_bookmaker
from sports_analytics.bookmakers.diagnostics.smoke import smoke_bookmaker
from sports_analytics.bookmakers.enqueue import enqueue_bookmaker_acquisition
from sports_analytics.bookmakers.loader import load_bookmaker_snapshot
from sports_analytics.bookmakers.types import (
    DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
)
from sports_analytics.core.cli import FAILURE_EXIT, SUCCESS_EXIT
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    PermanentSourceError,
    RepositoryError,
    SnapshotVerificationError,
)
from sports_analytics.core.runtime import bootstrap_runtime, validate_configuration
from sports_analytics.core.validation import (
    parse_cli_bounded_int,
    parse_cli_positive_bounded_int,
)
from sports_analytics.data.cli import inspect_database_status
from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.types import DEFAULT_JOB_PRIORITY, normalize_uuid
from sports_analytics.sources.betano.catalog import (
    BETANO_CATALOG,
)
from sports_analytics.sources.betano.catalog import (
    SUPPORTED_MARKET_DEFINITION_IDS as BETANO_MARKETS,
)
from sports_analytics.sources.betclic.catalog import (
    BETCLIC_CATALOG,
)
from sports_analytics.sources.betclic.catalog import (
    SUPPORTED_MARKET_DEFINITION_IDS as BETCLIC_MARKETS,
)
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS


def list_bookmaker_sports(*, as_json: bool = True) -> int:
    """Print supported bookmaker sports."""
    payload = {
        "sports": list(SUPPORTED_BOOKMAKER_SPORTS),
        "providers": [PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT],
    }
    if as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        for sport in SUPPORTED_BOOKMAKER_SPORTS:
            print(sport)
    return SUCCESS_EXIT


def list_bookmaker_markets(*, provider: str | None = None, as_json: bool = True) -> int:
    """Print supported market definition ids for one or all providers."""
    providers: Sequence[str]
    if provider is None:
        providers = (PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT)
    else:
        providers = (provider,)
    rows: list[dict[str, Any]] = []
    for provider_id in providers:
        if provider_id == PROVIDER_BETANO_PT:
            markets = BETANO_MARKETS
            display = BETANO_CATALOG.display_name
        elif provider_id == PROVIDER_BETCLIC_PT:
            markets = BETCLIC_MARKETS
            display = BETCLIC_CATALOG.display_name
        else:
            msg = f"unsupported bookmaker provider: {provider_id}"
            raise PermanentSourceError(msg)
        rows.append(
            {
                "provider_id": provider_id,
                "display_name": display,
                "markets": list(markets),
            }
        )
    if as_json:
        print(json.dumps({"providers": rows}, sort_keys=True, separators=(",", ":")))
    else:
        for row in rows:
            for market in row["markets"]:
                print(f"{row['provider_id']}\t{market}")
    return SUCCESS_EXIT


def provider_status(*, config: str | None, env_file: str | None) -> int:
    """Print current bookmaker provider status rows as JSON."""
    settings, paths = validate_configuration(config_path=config, env_file=env_file)
    status = inspect_database_status(settings, paths)
    if not status.is_up_to_date:
        msg = (
            f"database is not up to date: current={status.current_version} "
            f"latest={status.latest_version}"
        )
        raise DatabaseError(msg)
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        rows = BookmakerRepository(connection).list_provider_statuses()
    print(json.dumps({"providers": list(rows)}, sort_keys=True, separators=(",", ":")))
    return SUCCESS_EXIT


def list_bookmaker_snapshots(
    *,
    config: str | None,
    env_file: str | None,
    provider: str | None = None,
    sport: str | None = None,
) -> int:
    """List registered bookmaker snapshots (relative paths only)."""
    settings, paths = validate_configuration(config_path=config, env_file=env_file)
    status = inspect_database_status(settings, paths)
    if not status.is_up_to_date:
        msg = (
            f"database is not up to date: current={status.current_version} "
            f"latest={status.latest_version}"
        )
        raise DatabaseError(msg)
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        rows = BookmakerRepository(connection).list_snapshot_registrations(
            provider_id=provider,
            sport=sport,
        )
    print(json.dumps({"snapshots": list(rows)}, sort_keys=True, separators=(",", ":")))
    return SUCCESS_EXIT


def verify_bookmaker_snapshot(
    *,
    config: str | None,
    env_file: str | None,
    snapshot_id: str,
) -> int:
    """Verify one registered bookmaker snapshot without exposing absolute paths."""
    settings, paths = validate_configuration(config_path=config, env_file=env_file)
    status = inspect_database_status(settings, paths)
    if not status.is_up_to_date:
        msg = (
            f"database is not up to date: current={status.current_version} "
            f"latest={status.latest_version}"
        )
        raise DatabaseError(msg)
    try:
        normalized_id = normalize_uuid(snapshot_id)
    except RepositoryError as exc:
        raise SnapshotVerificationError(f"invalid snapshot id: {snapshot_id}") from exc
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        try:
            loaded = load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=paths.snapshots_directory,
                raw_directory=paths.raw_directory,
                snapshot_id=normalized_id,
            )
        except SnapshotVerificationError:
            raise
    payload = {
        "snapshot_id": loaded.snapshot_id,
        "verified": loaded.verified,
        "relative_path": loaded.relative_path,
        "checksum_sha256": loaded.checksum_sha256,
        "schema_version": loaded.schema_version,
        "provider_id": loaded.provider_id,
        "sport": loaded.sport,
        "registration_only": loaded.registration_only,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return SUCCESS_EXIT


def enqueue_bookmaker_acquisition_cli(
    *,
    config: str | None,
    env_file: str | None,
    provider: str,
    sport: str,
    priority: str | None = None,
    maximum_attempts: str | None = None,
) -> int:
    """Validate args, bootstrap, and enqueue one bookmaker acquisition job."""
    try:
        priority_value = DEFAULT_JOB_PRIORITY
        if priority is not None:
            priority_value = parse_cli_bounded_int(priority, field_name="priority")
        maximum_attempts_value = DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS
        if maximum_attempts is not None:
            maximum_attempts_value = parse_cli_positive_bounded_int(
                maximum_attempts,
                field_name="maximum_attempts",
            )
    except RepositoryError as exc:
        raise ConfigurationError(str(exc)) from exc

    runtime = bootstrap_runtime(
        "scraper",
        config_path=config,
        env_file=env_file,
    )
    job = enqueue_bookmaker_acquisition(
        database_path=runtime.database_path,
        bookmakers=runtime.settings.bookmakers,
        provider_id=provider,
        sport=sport,
        priority=priority_value,
        maximum_attempts=maximum_attempts_value,
        actor="scraper-cli",
        created_at=runtime.started_at,
    )
    print(
        json.dumps(
            {
                "enqueued": True,
                "job_id": job.id,
                "provider_id": provider,
                "sport": sport,
                "status": job.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return SUCCESS_EXIT


def probe_bookmaker_cli(
    *,
    provider: str,
    sport: str,
    duration_seconds: str | None = None,
    diagnostic_directory: str | None = None,
) -> int:
    """Run one visible localhost probe and print sanitized structural evidence."""
    duration = 30
    if duration_seconds is not None:
        duration = parse_cli_positive_bounded_int(duration_seconds, field_name="duration_seconds")
    result = probe_bookmaker(
        provider_id=provider,
        sport=sport,
        duration_seconds=duration,
        diagnostic_directory=diagnostic_directory,
    )
    print(
        json.dumps(
            {
                "provider": result.provider,
                "sport": result.sport,
                "duration_seconds": result.duration_seconds,
                "blocked": result.blocked,
                "block_reason": result.block_reason,
                "response_count": len(result.responses),
                "page_count": len(result.pages),
                "diagnostic_relative_path": result.diagnostic_relative_path,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return SUCCESS_EXIT


def smoke_bookmaker_cli(
    *,
    config: str | None,
    env_file: str | None,
    provider: str,
    sport: str,
    duration_seconds: str | None = None,
    diagnostic_directory: str | None = None,
) -> int:
    """Run one bounded provider smoke test."""
    runtime = bootstrap_runtime(
        "scraper",
        config_path=config,
        env_file=env_file,
    )
    duration = 60
    if duration_seconds is not None:
        duration = parse_cli_positive_bounded_int(duration_seconds, field_name="duration_seconds")
    result = smoke_bookmaker(
        provider_id=provider,
        sport=sport,
        duration_seconds=duration,
        diagnostic_directory=diagnostic_directory,
        database_path=runtime.database_path,
        raw_directory=runtime.paths.raw_directory,
        snapshots_directory=runtime.paths.snapshots_directory,
    )
    print(
        json.dumps(
            {
                "provider": result.provider,
                "sport": result.sport,
                "succeeded": result.succeeded,
                "failure_reason": result.failure_reason,
                "profile_id": result.profile_id,
                "profile_verified": result.profile_verified,
                "diagnostic_relative_path": result.diagnostic_relative_path,
                "acceptance_summary": result.acceptance_summary,
                "cycles": [
                    {
                        "cycle_number": cycle.cycle_number,
                        "acquisition_cycle_id": cycle.acquisition_cycle_id,
                        "events_extracted": cycle.events_extracted,
                        "valid_quote_count": cycle.valid_quote_count,
                        "snapshot_id": cycle.snapshot_id,
                        "snapshot_verified": cycle.snapshot_verified,
                        "snapshot_reused": cycle.snapshot_reused,
                        "duration_seconds": cycle.duration_seconds,
                    }
                    for cycle in result.cycles
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return SUCCESS_EXIT if result.succeeded else FAILURE_EXIT

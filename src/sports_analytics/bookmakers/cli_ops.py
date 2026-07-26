"""Bookmaker CLI operations for scrape coordinator integration."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sports_analytics.bookmakers.enqueue import enqueue_bookmaker_acquisition
from sports_analytics.bookmakers.schemas import bookmaker_snapshot_suite
from sports_analytics.bookmakers.types import (
    DEFAULT_BOOKMAKER_INGESTION_MAXIMUM_ATTEMPTS,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
)
from sports_analytics.core.cli import SUCCESS_EXIT
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
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import DEFAULT_JOB_PRIORITY, SnapshotStatus, normalize_uuid
from sports_analytics.snapshots.reader import verify_snapshot_directory
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
        registration = BookmakerRepository(connection).get_snapshot_registration(normalized_id)
        record = SnapshotRepository(connection).get_snapshot(normalized_id)
    if registration is None and record is None:
        raise SnapshotVerificationError(f"bookmaker snapshot not found: {normalized_id}")

    if record is not None:
        if record.status is not SnapshotStatus.READY:
            raise SnapshotVerificationError(
                f"snapshot {normalized_id} is not READY (status={record.status.value})"
            )
        sport_code = "football"
        if registration is not None:
            sport_code = str(registration["sport"])
        result = verify_snapshot_directory(
            snapshots_directory=paths.snapshots_directory,
            relative_manifest_path=record.relative_path,
            suite=bookmaker_snapshot_suite(sport_code=sport_code),
            expected_snapshot=record,
        )
        payload = {
            "snapshot_id": normalized_id,
            "verified": True,
            "relative_path": record.relative_path,
            "checksum_sha256": result.manifest_checksum_sha256,
            "schema_version": result.schema_version,
            "snapshot_type": result.snapshot_type,
            "provider_id": (None if registration is None else registration.get("provider_id")),
            "sport": sport_code,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return SUCCESS_EXIT

    assert registration is not None
    payload = {
        "snapshot_id": normalized_id,
        "verified": True,
        "relative_path": registration["relative_path"],
        "checksum_sha256": registration["checksum_sha256"],
        "schema_version": registration["schema_version"],
        "provider_id": registration["provider_id"],
        "sport": registration["sport"],
        "registration_only": True,
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

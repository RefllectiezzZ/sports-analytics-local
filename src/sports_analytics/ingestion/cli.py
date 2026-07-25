"""Data ingestion coordinator CLI for football source enqueue and verification."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    PermanentSourceError,
    RepositoryError,
    RuntimeBootstrapError,
    SnapshotVerificationError,
    SportsAnalyticsError,
)
from sports_analytics.core.runtime import bootstrap_runtime, validate_configuration
from sports_analytics.core.validation import parse_positive_decimal_int
from sports_analytics.data.cli import inspect_database_status
from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import (
    DEFAULT_JOB_PRIORITY,
    SnapshotStatus,
    normalize_uuid,
    validate_strict_int,
)
from sports_analytics.ingestion.football import enqueue_football_data_ingestion
from sports_analytics.ingestion.types import DEFAULT_INGESTION_MAXIMUM_ATTEMPTS
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sources.catalog import list_source_names
from sports_analytics.sources.football_data_co_uk.catalog import list_competitions


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the scraper CLI argument parser."""
    parser = build_common_argument_parser(
        "scraper",
        "Data ingestion coordinator for football source enqueue and verification.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list-sources",
        action="store_true",
        help="List static source identifiers without database or network access.",
    )
    mode.add_argument(
        "--list-competitions",
        action="store_true",
        help="List static competition catalog entries without database or network access.",
    )
    mode.add_argument(
        "--list-snapshots",
        action="store_true",
        help="List snapshot metadata from the operational database.",
    )
    mode.add_argument(
        "--verify-snapshot",
        metavar="SNAPSHOT_ID",
        default=None,
        help="Verify a READY snapshot filesystem integrity read-only.",
    )
    mode.add_argument(
        "--enqueue-football-data",
        action="store_true",
        help="Enqueue one ingest.football-data-csv job (worker performs download).",
    )
    parser.add_argument(
        "--competition",
        default=None,
        metavar="COMPETITION_ID",
        help="Competition ID for --enqueue-football-data.",
    )
    parser.add_argument(
        "--season",
        default=None,
        metavar="YYYY-YYYY",
        help="Canonical season for --enqueue-football-data.",
    )
    parser.add_argument(
        "--raw-sha256",
        default=None,
        metavar="SHA256",
        help="Optional content-addressed raw artifact hash for reprocessing.",
    )
    parser.add_argument(
        "--priority",
        default=None,
        metavar="INTEGER",
        help="Optional job priority integer (default 100).",
    )
    parser.add_argument(
        "--maximum-attempts",
        default=None,
        metavar="INTEGER",
        help="Optional maximum attempts (default 3).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scraper CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        _validate_modes(parser, args)
        common_exit = handle_common_modes(args)
        if common_exit is not None:
            return common_exit

        if args.list_sources:
            for name in list_source_names():
                print(name)
            return SUCCESS_EXIT

        if args.list_competitions:
            for entry in list_competitions():
                print(
                    f"{entry.competition_id}\t{entry.display_name}\t"
                    f"{entry.division_code}\t{entry.timezone}"
                )
            return SUCCESS_EXIT

        if args.list_snapshots:
            return _list_snapshots(args.config, args.env_file)

        if args.verify_snapshot is not None:
            return _verify_snapshot(args.config, args.env_file, args.verify_snapshot)

        if args.enqueue_football_data:
            return _enqueue(args)

        parser.error("select a scraper mode such as --list-competitions or --enqueue-football-data")
        return CONFIG_ERROR_EXIT
    except (
        ConfigurationError,
        RuntimeBootstrapError,
        DatabaseError,
        PermanentSourceError,
        SnapshotVerificationError,
        RepositoryError,
        SportsAnalyticsError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def _validate_modes(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    scraper_modes = [
        args.list_sources,
        args.list_competitions,
        args.list_snapshots,
        args.verify_snapshot is not None,
        args.enqueue_football_data,
    ]
    if sum(1 for enabled in scraper_modes if enabled) > 1:
        parser.error("scraper modes are mutually exclusive")
    common = args.validate_config or args.database_status or args.migrate_database
    enqueue_args = any(
        value is not None
        for value in (
            args.competition,
            args.season,
            args.raw_sha256,
            args.priority,
            args.maximum_attempts,
        )
    )
    if common and (any(scraper_modes) or enqueue_args):
        parser.error("scraper modes cannot be combined with shared CLI modes")
    if enqueue_args and not args.enqueue_football_data:
        parser.error("enqueue arguments require --enqueue-football-data")
    if args.enqueue_football_data and (args.competition is None or args.season is None):
        parser.error("--enqueue-football-data requires --competition and --season")


def _enqueue(args: argparse.Namespace) -> int:
    runtime = bootstrap_runtime(
        "scraper",
        config_path=args.config,
        env_file=args.env_file,
    )
    priority = DEFAULT_JOB_PRIORITY
    if args.priority is not None:
        try:
            priority = validate_strict_int(int(args.priority, 10), field_name="priority")
        except (ValueError, RepositoryError) as exc:
            raise ConfigurationError("priority must be a strict integer") from exc
    maximum_attempts = DEFAULT_INGESTION_MAXIMUM_ATTEMPTS
    if args.maximum_attempts is not None:
        try:
            maximum_attempts = parse_positive_decimal_int(
                args.maximum_attempts,
                field_name="maximum_attempts",
            )
        except RepositoryError as exc:
            raise ConfigurationError(str(exc)) from exc
    job = enqueue_football_data_ingestion(
        database_path=runtime.database_path,
        scraping=runtime.settings.scraping,
        competition_id=args.competition,
        season=args.season,
        raw_sha256=args.raw_sha256,
        priority=priority,
        maximum_attempts=maximum_attempts,
        actor="scraper-cli",
        created_at=runtime.started_at,
    )
    print(
        f"enqueued job_id={job.id} competition={args.competition} "
        f"season={args.season} status={job.status.value}"
    )
    return SUCCESS_EXIT


def _list_snapshots(config: str | None, env_file: str | None) -> int:
    settings, paths = validate_configuration(config_path=config, env_file=env_file)
    status = inspect_database_status(settings, paths)
    if not status.is_up_to_date:
        msg = (
            f"database is not up to date: current={status.current_version} "
            f"latest={status.latest_version}"
        )
        raise DatabaseError(msg)
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        snapshots = SnapshotRepository(connection).list_snapshots()
    for item in snapshots:
        source_version = item.source_version or "-"
        row_count = item.row_count if item.row_count is not None else "-"
        print(
            f"{item.id}\t{item.status.value}\t{item.source_name}\t"
            f"{source_version}\trows={row_count}\t{item.schema_version}"
        )
    return SUCCESS_EXIT


def _verify_snapshot(config: str | None, env_file: str | None, snapshot_id: str) -> int:
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
        record = SnapshotRepository(connection).get_snapshot(normalized_id)
    if record is None:
        raise SnapshotVerificationError(f"snapshot not found: {normalized_id}")
    if record.status is not SnapshotStatus.READY:
        raise SnapshotVerificationError(
            f"snapshot {normalized_id} is not READY (status={record.status.value})"
        )
    result = verify_snapshot_directory(
        snapshots_directory=paths.snapshots_directory,
        relative_manifest_path=record.relative_path,
        expected_snapshot=record,
    )
    print(
        f"verified snapshot_id={result.snapshot_id} "
        f"games={result.games_count} files={result.file_count} "
        f"manifest_sha256={result.manifest_checksum_sha256}"
    )
    return SUCCESS_EXIT

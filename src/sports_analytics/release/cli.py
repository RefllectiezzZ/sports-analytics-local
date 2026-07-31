"""Supported local v1 operator command."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sports_analytics import __version__
from sports_analytics.core.exceptions import SportsAnalyticsError
from sports_analytics.core.paths import create_runtime_directories, resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.data.migrations import ensure_database_ready, get_migration_status
from sports_analytics.local.supervisor import LocalSupervisor
from sports_analytics.release.backup import BackupError, create_backup, restore_backup
from sports_analytics.release.doctor import (
    PLACEMENT_MODE,
    SUPPORTED_CURRENT_PRICE_PATH,
    inspect_release_readiness,
)

SUCCESS_EXIT: Final[int] = 0
INVALID_EXIT: Final[int] = 2
NOT_INITIALIZED_EXIT: Final[int] = 3


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the closed, allowlisted local v1 argument surface."""
    parser = argparse.ArgumentParser(
        prog="sports-analytics-v1",
        description=(
            "Initialize, inspect, back up, restore, or run the localhost-only "
            "sports analytics v1 application."
        ),
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--version", action="store_true", help="Print the application version.")
    modes.add_argument(
        "--initialize",
        action="store_true",
        help="Create the configured local runtime and apply migrations 0001-0005.",
    )
    modes.add_argument(
        "--doctor",
        action="store_true",
        help="Inspect local v1 readiness without changing any state.",
    )
    modes.add_argument(
        "--backup",
        metavar="BACKUP_DIRECTORY",
        help="Create a new content-verified local v1 backup.",
    )
    modes.add_argument(
        "--restore",
        metavar="BACKUP_DIRECTORY",
        help="Restore a verified local v1 backup into empty configured state.",
    )
    parser.add_argument("--config", metavar="PATH", help="Use an explicit TOML configuration.")
    parser.add_argument("--env-file", metavar="PATH", help="Use an explicit dotenv file.")
    parser.add_argument(
        "--ui-port",
        type=_valid_port,
        default=8501,
        metavar="PORT",
        help="Loopback Streamlit port for normal launch (default: 8501).",
    )
    parser.add_argument(
        "--worker-once",
        action="store_true",
        help="Process at most one available job and exit without starting the UI.",
    )
    parser.add_argument(
        "--worker-max-jobs",
        type=_positive_int,
        metavar="COUNT",
        help="Stop the worker after COUNT claimed jobs.",
    )
    parser.add_argument("--worker-id", metavar="UUID", help="Use an explicit durable worker UUID.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the supported local v1 operator command."""
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if (
        args.version
        or args.initialize
        or args.doctor
        or args.backup is not None
        or args.restore is not None
    ) and (
        args.ui_port != 8501
        or args.worker_once
        or args.worker_max_jobs is not None
        or args.worker_id is not None
    ):
        parser.error("launch options cannot be combined with a one-shot release mode")
    if args.worker_once and args.worker_max_jobs is not None:
        parser.error("--worker-once and --worker-max-jobs cannot be combined")

    if args.version:
        print(__version__)
        return SUCCESS_EXIT
    try:
        if args.initialize:
            _print_json(initialize_v1(config_path=args.config, env_file=args.env_file))
            return SUCCESS_EXIT
        if args.doctor:
            report = inspect_release_readiness(
                config_path=args.config,
                env_file=args.env_file,
            )
            _print_json(report)
            if report["overall_state"] in {"ready", "degraded"}:
                return SUCCESS_EXIT
            if report["overall_state"] == "not-initialized":
                return NOT_INITIALIZED_EXIT
            return INVALID_EXIT

        settings = load_settings(config_path=args.config, env_file=args.env_file)
        paths = resolve_paths(settings, Path.cwd())
        if args.backup is not None:
            _print_json(
                create_backup(
                    args.backup,
                    paths=paths,
                    explicit_config=args.config,
                )
            )
            return SUCCESS_EXIT
        if args.restore is not None:
            _print_json(restore_backup(args.restore, paths=paths))
            return SUCCESS_EXIT

        # Normal launch is the primary MVP path. Initialization is deliberately
        # idempotent and never rewrites operator configuration.
        initialize_v1(
            config_path=args.config,
            env_file=args.env_file,
            base_directory=Path.cwd(),
        )
        print(f"http://127.0.0.1:{args.ui_port}", flush=True)
        return LocalSupervisor().run(
            config=args.config,
            env_file=args.env_file,
            worker_once=args.worker_once,
            worker_max_jobs=args.worker_max_jobs,
            worker_id=args.worker_id,
            start_ui=not args.worker_once,
            ui_port=args.ui_port,
        )
    except (SportsAnalyticsError, BackupError, OSError, ValueError) as exc:
        print(f"error: {_safe_detail(exc)}", file=sys.stderr)
        return INVALID_EXIT


def initialize_v1(
    *,
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    base_directory: Path | str | None = None,
) -> dict[str, Any]:
    """Create the idempotent local v1 runtime boundary and return its result."""
    base = Path.cwd() if base_directory is None else Path(base_directory)
    settings = load_settings(
        config_path=config_path,
        env_file=env_file,
        base_directory=base,
    )
    paths = resolve_paths(settings, base)
    directories = _runtime_directories(paths)
    existing = [str(path) for path in directories if path.is_dir()]
    create_runtime_directories(paths)
    created = [str(path) for path in directories if str(path) not in set(existing)]
    readiness = ensure_database_ready(paths.sqlite_path)
    status = get_migration_status(paths.sqlite_path)
    if not status.is_up_to_date or status.current_version != 5 or status.latest_version != 5:
        raise ValueError("initialization did not reach exact migration state 0005")
    from sports_analytics.mvp.automatic_market_data import ensure_startup_automatic_job

    automatic_job_id = ensure_startup_automatic_job(
        paths=paths,
        now=datetime.now(tz=UTC),
    )
    return {
        "already_existing_directories": sorted(existing),
        "application_version": __version__,
        "automatic_market_data_job_id": automatic_job_id,
        "bookmakers_enabled": settings.bookmakers.enabled,
        "config_source": (
            str(Path(config_path).resolve())
            if config_path is not None
            else "built-in-defaults-or-environment"
        ),
        "created_directories": sorted(created),
        "database_migration_state": {
            "applied_now": [item.version for item in readiness.migrations_applied],
            "current_version": status.current_version,
            "latest_version": status.latest_version,
            "up_to_date": status.is_up_to_date,
        },
        "database_path": str(paths.sqlite_path),
        "placement_mode": PLACEMENT_MODE,
        "state": "initialized",
        "storage_root": str(paths.storage_root),
        "supported_current_price_path": SUPPORTED_CURRENT_PRICE_PATH,
    }


def _runtime_directories(paths: Any) -> tuple[Path, ...]:
    ordered = (
        paths.storage_root,
        paths.sqlite_path.parent,
        paths.raw_directory,
        paths.snapshots_directory,
        paths.features_directory,
        paths.models_directory,
        paths.exports_directory,
        paths.logs_directory,
    )
    return tuple(dict.fromkeys(ordered))


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _valid_port(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 1 through 65535") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be an integer from 1 through 65535")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _safe_detail(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or type(exc).__name__


if __name__ == "__main__":
    raise SystemExit(main())

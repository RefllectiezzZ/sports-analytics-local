"""Shared database CLI helpers for root entry points."""

from __future__ import annotations

from sports_analytics.core.exceptions import DatabaseConnectionError, DatabaseMigrationError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.core.settings import Settings
from sports_analytics.data.database import verify_sqlite_file
from sports_analytics.data.migrations import (
    apply_migrations,
    format_database_status,
    get_migration_status,
)
from sports_analytics.data.types import DatabaseReadiness, MigrationStatus


def inspect_database_status(settings: Settings, paths: RuntimePaths) -> MigrationStatus:
    """Inspect an existing database read-only without creating files."""
    del settings  # settings already validated by the caller
    path = paths.sqlite_path
    if not path.exists():
        msg = f"SQLite database file does not exist: {path}"
        raise DatabaseConnectionError(msg)
    if path.is_dir():
        msg = f"SQLite path points to a directory: {path}"
        raise DatabaseConnectionError(msg)
    verify_sqlite_file(path, quick=True)
    return get_migration_status(path)


def migrate_database(settings: Settings, paths: RuntimePaths) -> DatabaseReadiness:
    """Create the database parent directory if needed and apply migrations."""
    del settings
    path = paths.sqlite_path
    if path.exists() and path.is_dir():
        msg = f"SQLite path points to a directory: {path}"
        raise DatabaseConnectionError(msg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"unable to create SQLite parent directory {path.parent}: {exc}"
        raise DatabaseConnectionError(msg) from exc
    return apply_migrations(path)


def format_migration_result(result: DatabaseReadiness) -> str:
    """Return concise human-readable migration output."""
    applied_names = ", ".join(item.filename for item in result.migrations_applied) or "(none)"
    return (
        "database migrated: "
        f"path={result.database_path} "
        f"previous_version={result.previous_version} "
        f"final_version={result.schema_version} "
        f"migrations_applied={applied_names}"
    )


def format_status_or_raise(status: MigrationStatus) -> str:
    """Format status, raising when history is inconsistent or outdated unexpectedly."""
    if not status.checksums_valid:
        msg = f"database migration checksums are invalid for {status.database_path}"
        raise DatabaseMigrationError(msg)
    return format_database_status(status)

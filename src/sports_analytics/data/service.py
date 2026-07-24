"""Database initialization service for local runtime bootstrap."""

from __future__ import annotations

from pathlib import Path

from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.types import DatabaseReadiness


def initialize_operational_database(
    paths_or_sqlite: RuntimePaths | Path | str,
) -> DatabaseReadiness:
    """Ensure the operational SQLite database is migrated and ready.

    Accepts ``RuntimePaths`` or an explicit SQLite path. Does not retain an open
    connection and does not insert demo or domain records.
    """
    if isinstance(paths_or_sqlite, RuntimePaths):
        database_path = paths_or_sqlite.sqlite_path
    else:
        database_path = Path(paths_or_sqlite)
    return ensure_database_ready(database_path)

"""Shared helpers for data-layer unit tests."""

from __future__ import annotations

from pathlib import Path

from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready


def migrated_database(tmp_path: Path) -> Path:
    """Create a temporary migrated operational database and return its path."""
    database_path = tmp_path / "operational.sqlite3"
    ensure_database_ready(database_path)
    return database_path


def open_migrated(tmp_path: Path):
    """Context-manager helper returning a writable migrated connection factory path."""
    return migrated_database(tmp_path)


__all__ = ["connect_database", "migrated_database", "open_migrated"]

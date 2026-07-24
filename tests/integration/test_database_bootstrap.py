"""Integration tests for database bootstrap behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_analytics.core.exceptions import RuntimeBootstrapError
from sports_analytics.core.logging import reset_logging
from sports_analytics.core.runtime import bootstrap_runtime, validate_configuration
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready


def test_normal_bootstrap_migrates_and_reports_schema(tmp_path: Path) -> None:
    context = bootstrap_runtime(
        "engine",
        environ={},
        base_directory=tmp_path,
        overrides={"logging": {"file_enabled": False}},
    )
    assert context.schema_version == 2
    assert context.database_path.is_file()
    assert context.paths.sqlite_path.is_file()
    # RuntimeContext must not retain an open connection attribute beyond path metadata.
    assert not hasattr(context, "connection")
    second = bootstrap_runtime(
        "engine",
        environ={},
        base_directory=tmp_path,
        overrides={"logging": {"file_enabled": False}},
    )
    assert second.schema_version == 2
    reset_logging()
    context.paths.sqlite_path.unlink()
    wal = Path(str(context.paths.sqlite_path) + "-wal")
    shm = Path(str(context.paths.sqlite_path) + "-shm")
    if wal.exists():
        wal.unlink()
    if shm.exists():
        shm.unlink()


def test_validation_only_creates_no_sqlite(tmp_path: Path) -> None:
    _, paths = validate_configuration(environ={}, base_directory=tmp_path)
    assert not paths.sqlite_path.exists()
    assert not paths.storage_root.exists()


def test_invalid_migration_history_causes_bootstrap_failure(tmp_path: Path) -> None:
    ensure_database_ready(tmp_path / "storage" / "operational.sqlite3")
    db = tmp_path / "storage" / "operational.sqlite3"
    with connect_database(db) as connection:
        with transaction(connection):
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
                ("0" * 64,),
            )
    with pytest.raises(RuntimeBootstrapError, match="database initialization failed"):
        bootstrap_runtime(
            "engine",
            environ={},
            base_directory=tmp_path,
            overrides={"logging": {"file_enabled": False}},
        )
    reset_logging()

"""Focused tests for migration 0002 worker runtime additions."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import (
    apply_migrations,
    compute_migration_checksum,
    discover_migrations,
    ensure_database_ready,
    split_sql_statements,
)
from sports_analytics.data.schema import EXPECTED_INDEXES, EXPECTED_TRIGGERS, OPERATIONAL_TABLES

CHECKSUM_0001 = "404e1c0b36390ff7a42de901f344edcb60b9cee248b741116bc9d47a17cf48de"
CHECKSUM_0002 = "b3a8d93ae81ce2e21ae9e74a420bf598b345d63fe4ed11d4d84ced6302021faa"


def test_migration_0002_discovered_with_expected_checksum() -> None:
    migrations = discover_migrations()
    assert [migration.version for migration in migrations] == [1, 2]
    assert migrations[0].checksum == CHECKSUM_0001
    assert migrations[1].filename == "0002_worker_runtime.sql"
    assert migrations[1].name == "worker_runtime"
    assert migrations[1].checksum == CHECKSUM_0002

    text = (
        resources.files("sports_analytics.data.sql.migrations")
        .joinpath("0002_worker_runtime.sql")
        .read_text(encoding="utf-8")
    )
    assert compute_migration_checksum(text) == CHECKSUM_0002
    statements = split_sql_statements(text)
    assert sum(1 for statement in statements if statement.upper().startswith("CREATE TRIGGER")) == 2


def test_fresh_schema_contains_worker_runtime_objects(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    readiness = ensure_database_ready(db)
    assert readiness.schema_version == 2

    with connect_database(db, read_only=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert set(OPERATIONAL_TABLES).issubset(tables)
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(worker_instances)").fetchall()
        }
        assert {
            "id",
            "status",
            "process_id",
            "heartbeat_at",
            "current_job_id",
            "capabilities_json",
            "version",
        }.issubset(columns)
        assert columns["id"]["pk"] == 1

        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert set(EXPECTED_INDEXES).issubset(indexes)

        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert set(EXPECTED_TRIGGERS).issubset(triggers)


def test_running_job_lease_triggers_reject_inconsistent_rows(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        base = (
            "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
            "maximum_attempts, available_at, lease_owner, lease_expires_at, created_at, "
            "updated_at, started_at, finished_at, version) VALUES "
        )
        with pytest.raises(sqlite3.IntegrityError, match="running job requires complete lease"):
            connection.execute(
                base + "('j1', 'demo.job', '{}', 'running', 100, 1, 2, 't', NULL, NULL, "
                "'t', 't', 't', NULL, 1)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="non-running job must not retain a lease"):
            connection.execute(
                base + "('j2', 'demo.job', '{}', 'pending', 100, 0, 2, 't', 'worker', "
                "'future', 't', 't', NULL, NULL, 1)"
            )

        connection.execute(
            base + "('j3', 'demo.job', '{}', 'running', 100, 1, 2, 't', 'worker', "
            "'future', 't', 't', 't', NULL, 1)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="running job requires complete lease"):
            connection.execute("UPDATE jobs SET lease_owner = NULL WHERE id = 'j3'")
        with pytest.raises(sqlite3.IntegrityError, match="non-running job must not retain a lease"):
            connection.execute(
                "UPDATE jobs SET status = 'failed', finished_at = 't' WHERE id = 'j3'"
            )


def test_upgrade_from_version_1_applies_only_0002(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    migrations = discover_migrations()
    first = apply_migrations(db, migrations=(migrations[0],))
    assert first.schema_version == 1
    with connect_database(db, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'worker_instances'"
            ).fetchone()
            is None
        )

    upgraded = apply_migrations(db, migrations=migrations)
    assert upgraded.previous_version == 1
    assert [migration.version for migration in upgraded.migrations_applied] == [2]
    assert upgraded.schema_version == 2
    with connect_database(db, read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'worker_instances'"
            ).fetchone()
            is not None
        )

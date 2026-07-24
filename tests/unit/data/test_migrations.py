"""Tests for the internal forward-only migration system."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from importlib import resources
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseMigrationError
from sports_analytics.data import migrations as migrations_module
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import (
    apply_migrations,
    compute_migration_checksum,
    discover_migrations,
    ensure_database_ready,
    get_migration_status,
    split_sql_statements,
    validate_migration_sql,
)
from sports_analytics.data.schema import EXPECTED_INDEXES, EXPECTED_TRIGGERS, OPERATIONAL_TABLES
from sports_analytics.data.types import Migration


class _FakeFile:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text

    def is_file(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8") -> str:
        del encoding
        return self._text


class _FakeRoot:
    def __init__(self, files: list[_FakeFile]) -> None:
        self._files = files

    def iterdir(self):
        return iter(self._files)


def test_migration_discovery_deterministic_and_from_package() -> None:
    first = discover_migrations()
    second = discover_migrations()
    assert first == second
    assert [migration.version for migration in first] == [1, 2]
    assert [migration.filename for migration in first] == [
        "0001_initial.sql",
        "0002_worker_runtime.sql",
    ]
    assert first[0].checksum == ("404e1c0b36390ff7a42de901f344edcb60b9cee248b741116bc9d47a17cf48de")
    assert first[1].checksum == ("b3a8d93ae81ce2e21ae9e74a420bf598b345d63fe4ed11d4d84ced6302021faa")
    packaged = resources.files("sports_analytics.data.sql.migrations").joinpath("0001_initial.sql")
    assert packaged.is_file()
    text = packaged.read_text(encoding="utf-8")
    assert "CREATE TABLE application_metadata" in text
    assert (
        first[0].checksum
        == hashlib.sha256(
            text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        ).hexdigest()
    )
    packaged_0002 = resources.files("sports_analytics.data.sql.migrations").joinpath(
        "0002_worker_runtime.sql"
    )
    assert packaged_0002.is_file()
    assert "CREATE TABLE worker_instances" in packaged_0002.read_text(encoding="utf-8")


def test_malformed_duplicate_gap_and_prohibited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DatabaseMigrationError, match="prohibited"):
        validate_migration_sql("BEGIN; CREATE TABLE x(id INTEGER);", filename="0002_x.sql")
    with pytest.raises(DatabaseMigrationError, match="prohibited"):
        validate_migration_sql("COMMIT;", filename="0002_x.sql")
    with pytest.raises(DatabaseMigrationError, match="prohibited"):
        validate_migration_sql("PRAGMA journal_mode=OFF;", filename="0002_x.sql")

    monkeypatch.setattr(
        migrations_module.resources,
        "files",
        lambda package: _FakeRoot([_FakeFile("bad.sql", "CREATE TABLE x(id INTEGER);")]),
    )
    with pytest.raises(DatabaseMigrationError, match="malformed"):
        discover_migrations()

    monkeypatch.setattr(
        migrations_module.resources,
        "files",
        lambda package: _FakeRoot(
            [
                _FakeFile("0001_one.sql", "CREATE TABLE one(id INTEGER);"),
                _FakeFile("0001_two.sql", "CREATE TABLE two(id INTEGER);"),
            ]
        ),
    )
    with pytest.raises(DatabaseMigrationError, match="duplicate"):
        discover_migrations()

    monkeypatch.setattr(
        migrations_module.resources,
        "files",
        lambda package: _FakeRoot(
            [
                _FakeFile("0001_one.sql", "CREATE TABLE one(id INTEGER);"),
                _FakeFile("0003_three.sql", "CREATE TABLE three(id INTEGER);"),
            ]
        ),
    )
    with pytest.raises(DatabaseMigrationError, match="consecutive"):
        discover_migrations()


def test_fresh_database_migrates_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    first = ensure_database_ready(db)
    assert first.schema_version == 2
    assert first.previous_version == 0
    assert [migration.version for migration in first.migrations_applied] == [1, 2]
    second = ensure_database_ready(db)
    assert second.schema_version == 2
    assert second.previous_version == 2
    assert second.migrations_applied == ()
    status = get_migration_status(db)
    assert status.is_up_to_date
    assert status.checksums_valid
    assert status.current_version == 2
    assert status.pending == ()


def test_schema_tables_indexes_and_constraints(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for name in OPERATIONAL_TABLES:
            assert name in tables
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for name in EXPECTED_INDEXES:
            assert name in indexes
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        for name in EXPECTED_TRIGGERS:
            assert name in triggers

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
                "maximum_attempts, available_at, created_at, updated_at, version) "
                "VALUES ('j1', 't', '{}', 'nope', 100, 0, 1, 't', 't', 't', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
                "maximum_attempts, available_at, created_at, updated_at, version) "
                "VALUES ('j2', 't', '{}', 'pending', 100, 2, 1, 't', 't', 't', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
                "maximum_attempts, available_at, lease_owner, lease_expires_at, "
                "created_at, updated_at, version) "
                "VALUES ('j3', 't', '{}', 'pending', 100, 0, 1, 't', 'owner', NULL, 't', 't', 1)"
            )
        connection.execute(
            "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
            "maximum_attempts, available_at, created_at, updated_at, idempotency_key, version) "
            "VALUES ('j4', 't', '{}', 'pending', 100, 0, 1, 't', 't', 't', 'same', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
                "maximum_attempts, available_at, created_at, updated_at, idempotency_key, version) "
                "VALUES ('j5', 't', '{}', 'pending', 100, 0, 1, 't', 't', 't', 'same', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO snapshots(id, snapshot_type, status, relative_path, source_name, "
                "schema_version, created_at, metadata_json, version, ready_at, checksum_sha256, "
                "row_count) VALUES ('s1', 'raw', 'ready', 'a.parquet', 'src', 'v1', 't', '{}', 1, "
                "NULL, NULL, NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO snapshots(id, snapshot_type, status, relative_path, source_name, "
                "schema_version, created_at, metadata_json, version, checksum_sha256) "
                "VALUES ('s2', 'raw', 'building', 'b.parquet', 'src', 'v1', 't', '{}', 1, 'ZZ')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO snapshots(id, snapshot_type, status, relative_path, source_name, "
                "schema_version, created_at, metadata_json, version, row_count) "
                "VALUES ('s3', 'raw', 'building', 'c.parquet', 'src', 'v1', 't', '{}', 1, -1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_events(job_id, event_type, details_json, occurred_at, actor, "
                "job_version) VALUES ('missing', 'created', '{}', 't', 'cli', 1)"
            )
        connection.execute(
            "INSERT INTO application_metadata(key, value, updated_at) VALUES ('a', '1', 't')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO application_metadata(key, value, updated_at) VALUES ('a', '2', 't')"
            )


def test_changed_checksum_and_name_detected(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    original = discover_migrations()[0]
    ensure_database_ready(db)
    altered_checksum = Migration(
        version=original.version,
        name=original.name,
        sql_text=original.sql_text,
        checksum="0" * 64,
        filename=original.filename,
    )
    with pytest.raises(DatabaseMigrationError, match="checksum"):
        get_migration_status(db, migrations=(altered_checksum,))
    altered_name = Migration(
        version=original.version,
        name="renamed",
        sql_text=original.sql_text,
        checksum=original.checksum,
        filename=original.filename,
    )
    with pytest.raises(DatabaseMigrationError, match="name mismatch"):
        get_migration_status(db, migrations=(altered_name,))


def test_database_newer_and_missing_packaged_rejected(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection):
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at, "
                "execution_time_ms) VALUES (3, 'future', ?, '2026-07-24T00:00:00.000000Z', 1)",
                ("c" * 64,),
            )
    with pytest.raises(DatabaseMigrationError, match="newer"):
        get_migration_status(db)

    db2 = tmp_path / "ops2.sqlite3"
    ensure_database_ready(db2)
    packaged_without_v1 = (
        Migration(
            version=2,
            name="later",
            sql_text="CREATE TABLE later(id INTEGER);",
            checksum=compute_migration_checksum("CREATE TABLE later(id INTEGER);"),
            filename="0002_later.sql",
        ),
    )
    with pytest.raises(DatabaseMigrationError, match="consecutive|start"):
        get_migration_status(db2, migrations=packaged_without_v1)


def test_migration_failure_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    bad = Migration(
        version=1,
        name="bad",
        sql_text=(
            "CREATE TABLE ok(id INTEGER PRIMARY KEY);\nCREATE TABLE ok(id INTEGER PRIMARY KEY);"
        ),
        checksum=hashlib.sha256(b"x").hexdigest(),
        filename="0001_bad.sql",
    )
    with pytest.raises(DatabaseMigrationError):
        apply_migrations(db, migrations=(bad,))
    if db.exists():
        with connect_database(db) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if exists is not None:
                count = connection.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()
                assert count is not None and int(count["c"]) == 0
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ok'"
                ).fetchone()
                is None
            )


def test_simultaneous_migration_attempts(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            readiness = ensure_database_ready(db)
            results.append(readiness.schema_version)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert len(results) + len(errors) == 2
    # Busy errors are acceptable; successful completions must report version 2 once each.
    assert all(version == 2 for version in results)
    if results:
        status = get_migration_status(db)
        assert status.current_version == 2
        with connect_database(db, read_only=True) as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM schema_migrations").fetchone()
            assert count is not None and int(count["c"]) == 2


def test_split_sql_and_cwd_independence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    statements = split_sql_statements(
        "CREATE TABLE a(id INTEGER);\n-- comment\nCREATE TABLE b(id INTEGER);"
    )
    assert len(statements) == 2
    monkeypatch.chdir(tmp_path)
    migrations = discover_migrations()
    assert migrations[0].filename == "0001_initial.sql"
    assert migrations[1].filename == "0002_worker_runtime.sql"

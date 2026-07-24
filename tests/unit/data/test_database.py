"""Tests for SQLite connection factory and transactions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseConnectionError, DatabaseError
from sports_analytics.data.database import connect_database, transaction


def test_writable_connection_creates_database_and_configures(tmp_path: Path) -> None:
    path = tmp_path / "db" / "ops.sqlite3"
    with connect_database(path) as connection:
        assert path.is_file()
        assert connection.row_factory is sqlite3.Row
        fk = connection.execute("PRAGMA foreign_keys").fetchone()
        assert fk is not None and int(fk[0]) == 1
        busy = connection.execute("PRAGMA busy_timeout").fetchone()
        assert busy is not None and int(busy[0]) == 30_000
        journal = connection.execute("PRAGMA journal_mode").fetchone()
        assert journal is not None and str(journal[0]).lower() == "wal"
        row = connection.execute("SELECT 1 AS value").fetchone()
        assert row is not None
        assert row["value"] == 1


def test_read_only_rejects_missing_and_does_not_create(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(DatabaseConnectionError, match="does not exist"):
        with connect_database(path, read_only=True):
            pass
    assert not path.exists()


def test_connection_closes_after_context(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite3"
    with connect_database(path) as connection:
        raw = connection
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_directory_path_fails(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(DatabaseConnectionError, match="directory"):
        with connect_database(directory):
            pass


def test_invalid_database_file_fails(tmp_path: Path) -> None:
    path = tmp_path / "not-db.sqlite3"
    path.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(DatabaseConnectionError, match="non-SQLite|not a valid"):
        with connect_database(path):
            pass


def test_no_global_connection_and_separate_calls(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite3"
    with connect_database(path) as first:
        with connect_database(path) as second:
            assert first is not second
            assert first != second


def test_transaction_commit_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite3"
    with connect_database(path) as connection:
        connection.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        with transaction(connection):
            connection.execute("INSERT INTO demo(value) VALUES ('ok')")
        rows = connection.execute("SELECT value FROM demo").fetchall()
        assert [row["value"] for row in rows] == ["ok"]

        with pytest.raises(RuntimeError, match="boom"):
            with transaction(connection):
                connection.execute("INSERT INTO demo(value) VALUES ('nope')")
                raise RuntimeError("boom")
        rows = connection.execute("SELECT value FROM demo").fetchall()
        assert [row["value"] for row in rows] == ["ok"]


def test_immediate_transaction_and_nested_rejection(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite3"
    with connect_database(path) as connection:
        connection.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY)")
        with transaction(connection, immediate=True):
            connection.execute("INSERT INTO demo DEFAULT VALUES")
            with pytest.raises(DatabaseError, match="nested transaction"):
                with transaction(connection):
                    pass


def test_read_only_connection_on_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite3"
    with connect_database(path) as writable:
        writable.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY)")
        with transaction(writable):
            writable.execute("INSERT INTO demo DEFAULT VALUES")
    with connect_database(path, read_only=True) as readonly:
        count = readonly.execute("SELECT COUNT(*) AS c FROM demo").fetchone()
        assert count is not None and int(count["c"]) == 1
        journal = readonly.execute("PRAGMA journal_mode").fetchone()
        assert journal is not None

"""Regression tests for review fixes to the SQLite persistence layer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, run_component
from sports_analytics.core.exceptions import (
    DatabaseConnectionError,
    DatabaseMigrationError,
    RepositoryError,
)
from sports_analytics.data.database import (
    SQLITE_HEADER,
    connect_database,
    read_sqlite_header,
    require_active_transaction,
    transaction,
)
from sports_analytics.data.migrations import (
    compute_migration_checksum,
    ensure_database_ready,
    get_migration_status,
    split_sql_statements,
    validate_migration_sequence,
    validate_migration_sql,
)
from sports_analytics.data.repositories.application import ApplicationMetadataRepository
from sports_analytics.data.repositories.audit import AuditEventRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import (
    JobStatus,
    Migration,
    validate_relative_snapshot_path,
    validate_strict_int,
)

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)


def test_header_inspection_does_not_use_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)

    def _boom(self: Path) -> bytes:
        raise AssertionError("Path.read_bytes must not be used for header inspection")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    header = read_sqlite_header(db)
    assert header == SQLITE_HEADER
    with connect_database(db) as connection:
        assert connection.execute("SELECT 1").fetchone() is not None
    with connect_database(db, read_only=True) as connection:
        assert connection.execute("SELECT 1").fetchone() is not None


def test_invalid_header_rejected_without_full_read(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite3"
    path.write_bytes(b"not-a-sqlite-header-plus-extra-bytes" * 100)
    with pytest.raises(DatabaseConnectionError, match="not a valid SQLite|non-SQLite"):
        with connect_database(path):
            pass


def test_caller_sqlite_errors_are_not_connection_errors(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO application_metadata(key, value, updated_at) VALUES ('a', '1', 't')"
            )
            connection.execute(
                "INSERT INTO application_metadata(key, value, updated_at) VALUES ('a', '2', 't')"
            )
        assert not connection.in_transaction


def test_connection_closes_after_caller_failure(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with pytest.raises(sqlite3.OperationalError):
        with connect_database(db) as connection:
            raw = connection
            connection.execute("SELECT * FROM definitely_missing_table")
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_require_active_transaction_helper(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            require_active_transaction(connection, operation="demo.write")
        with transaction(connection):
            require_active_transaction(connection, operation="demo.write")


@pytest.mark.parametrize(
    ("repo_factory", "write"),
    [
        (
            ApplicationMetadataRepository,
            lambda repo: repo.upsert("k", "v", FIXED),
        ),
        (
            JobRepository,
            lambda repo: repo.create_job(
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            ),
        ),
        (
            SnapshotRepository,
            lambda repo: repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/file.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED,
            ),
        ),
        (
            AuditEventRepository,
            lambda repo: repo.append_event(
                event_type="demo.event",
                entity_type="demo",
                actor="cli",
                details={},
                occurred_at=FIXED,
            ),
        ),
    ],
)
def test_repository_writes_require_transaction(
    tmp_path: Path,
    repo_factory: type,
    write,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = repo_factory(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            write(repo)
        with transaction(connection):
            write(repo)


def test_create_job_and_transition_atomic_with_events(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with pytest.raises(RuntimeError):
            with transaction(connection):
                job = repo.create_job(
                    job_type="demo.job",
                    payload={"a": 1},
                    maximum_attempts=2,
                    actor="cli",
                    created_at=FIXED,
                )
                repo.transition_job(
                    job.id,
                    expected_status=JobStatus.PENDING,
                    expected_version=1,
                    new_status=JobStatus.RUNNING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=1),
                )
                raise RuntimeError("rollback")
        assert repo.count_jobs() == 0
        assert connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"] == 0


def test_applied_migration_gap_rejected(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection):
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at, "
                "execution_time_ms) VALUES (3, 'gap', ?, '2026-07-24T00:00:00.000000Z', 1)",
                ("a" * 64,),
            )
    with pytest.raises(DatabaseMigrationError, match="consecutive prefix"):
        get_migration_status(db)


def test_malformed_applied_timestamp_is_project_error(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with transaction(connection):
            connection.execute(
                "UPDATE schema_migrations SET applied_at = 'not-a-timestamp' WHERE version = 1"
            )
    with pytest.raises(DatabaseMigrationError, match="malformed|timestamp"):
        get_migration_status(db)


def test_database_status_cli_on_corrupt_history(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "db/ops.sqlite3"\n',
        encoding="utf-8",
    )
    db = isolated_cwd / "db" / "ops.sqlite3"
    ensure_database_ready(db)
    before = db.read_bytes()
    with connect_database(db) as connection:
        with transaction(connection):
            connection.execute("UPDATE schema_migrations SET applied_at = 'bad' WHERE version = 1")
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(config), "--database-status"],
    )
    assert code == CONFIG_ERROR_EXIT
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err
    # Status inspection must not repair or rewrite history.
    with connect_database(db, read_only=True) as connection:
        row = connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = 1"
        ).fetchone()
        assert row is not None and str(row["applied_at"]) == "bad"
    del before


def test_custom_migration_sequence_validation() -> None:
    sql = "CREATE TABLE demo(id INTEGER PRIMARY KEY);"
    checksum = compute_migration_checksum(sql)
    valid = Migration(
        version=1,
        name="demo",
        sql_text=sql,
        checksum=checksum,
        filename="0001_demo.sql",
    )
    assert validate_migration_sequence((valid,)) == (valid,)

    out_of_order = (
        Migration(
            version=2,
            name="second",
            sql_text=sql,
            checksum=checksum,
            filename="0002_second.sql",
        ),
        valid,
    )
    ordered = validate_migration_sequence(out_of_order)
    assert [item.version for item in ordered] == [1, 2]
    with pytest.raises(DatabaseMigrationError, match="duplicate"):
        validate_migration_sequence((valid, valid))
    with pytest.raises(DatabaseMigrationError, match="consecutive"):
        validate_migration_sequence(
            (
                valid,
                Migration(
                    version=3,
                    name="gap",
                    sql_text=sql,
                    checksum=checksum,
                    filename="0003_gap.sql",
                ),
            )
        )
    with pytest.raises(DatabaseMigrationError, match="positive"):
        validate_migration_sequence(
            (
                Migration(
                    version=0,
                    name="zero",
                    sql_text=sql,
                    checksum=checksum,
                    filename="0000_zero.sql",
                ),
            )
        )
    with pytest.raises(DatabaseMigrationError, match="filename/version/name"):
        validate_migration_sequence(
            (
                Migration(
                    version=1,
                    name="demo",
                    sql_text=sql,
                    checksum=checksum,
                    filename="0001_other.sql",
                ),
            )
        )
    with pytest.raises(DatabaseMigrationError, match="checksum"):
        validate_migration_sequence(
            (
                Migration(
                    version=1,
                    name="demo",
                    sql_text=sql,
                    checksum="0" * 64,
                    filename="0001_demo.sql",
                ),
            )
        )


def test_sql_parser_comments_quotes_and_prohibited() -> None:
    with pytest.raises(DatabaseMigrationError, match="prohibited"):
        validate_migration_sql("/* comment */ BEGIN;", filename="0002_x.sql")
    with pytest.raises(DatabaseMigrationError, match="prohibited"):
        validate_migration_sql(
            "/* multi\nline */ PRAGMA journal_mode=OFF;",
            filename="0002_x.sql",
        )
    statements = split_sql_statements(
        "CREATE TABLE a(id INTEGER, note TEXT DEFAULT 'a;b');\n"
        'CREATE TABLE "weird;name"(id INTEGER);\n'
        "CREATE TABLE [bracket;name](id INTEGER);\n"
        "CREATE TABLE `tick;name`(id INTEGER);"
    )
    assert len(statements) == 4
    with pytest.raises(DatabaseMigrationError, match="unclosed block comment"):
        split_sql_statements("CREATE TABLE a(id INTEGER); /* forever")
    with pytest.raises(DatabaseMigrationError, match="unclosed"):
        split_sql_statements("CREATE TABLE a(id INTEGER, note TEXT DEFAULT 'oops);")


def test_strict_integer_validation() -> None:
    assert validate_strict_int(0, field_name="offset", minimum=0) == 0
    assert validate_strict_int(3, field_name="limit", minimum=0) == 3
    with pytest.raises(RepositoryError):
        validate_strict_int(True, field_name="priority")
    with pytest.raises(RepositoryError):
        validate_strict_int(1.0, field_name="priority")
    with pytest.raises(RepositoryError):
        validate_strict_int(1.5, field_name="priority")
    with pytest.raises(RepositoryError):
        validate_strict_int("1", field_name="priority")
    with pytest.raises(RepositoryError):
        validate_strict_int(-1, field_name="offset", minimum=0)


def test_sqlite_storage_class_rejects_real_integers(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs(id, job_type, payload_json, status, priority, attempts, "
                "maximum_attempts, available_at, created_at, updated_at, version) "
                "VALUES ('j1', 't', '{}', 'pending', 1.5, 0, 1, 't', 't', 't', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO snapshots(id, snapshot_type, status, relative_path, source_name, "
                "schema_version, created_at, metadata_json, version, row_count) "
                "VALUES ('s1', 'raw', 'building', 'raw/a.parquet', 'src', 'v1', 't', '{}', 1, 1.5)"
            )


def test_snapshot_raw_path_validation() -> None:
    assert validate_relative_snapshot_path("raw/2026/data.parquet") == "raw/2026/data.parquet"
    rejected = [
        "",
        " raw/a.parquet",
        "/abs/path.parquet",
        "raw\\a.parquet",
        "raw//file.parquet",
        "raw/",
        "raw/./x.parquet",
        "raw/../x.parquet",
        "C:/file.parquet",
        "C:file.parquet",
        "raw/\x00bad.parquet",
    ]
    for value in rejected:
        with pytest.raises(RepositoryError):
            validate_relative_snapshot_path(value)


def test_retry_limits_cannot_create_unstartable_pending_job(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with transaction(connection):
            job = repo.create_job(
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            running = repo.transition_job(
                job.id,
                expected_status=JobStatus.PENDING,
                expected_version=1,
                new_status=JobStatus.RUNNING,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=1),
            )
            with pytest.raises(RepositoryError, match="retry=True"):
                repo.transition_job(
                    running.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=2,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=2),
                )
            with pytest.raises(RepositoryError, match="maximum_attempts"):
                repo.transition_job(
                    running.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=2,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=2),
                    retry=True,
                )
            failed = repo.transition_job(
                running.id,
                expected_status=JobStatus.RUNNING,
                expected_version=2,
                new_status=JobStatus.FAILED,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=3),
                last_error="boom",
            )
            before_events = connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()[
                "c"
            ]
            with pytest.raises(RepositoryError, match="maximum_attempts"):
                repo.transition_job(
                    failed.id,
                    expected_status=JobStatus.FAILED,
                    expected_version=3,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=4),
                    retry=True,
                )
            assert repo.get_job(failed.id) == failed
            assert (
                connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"]
                == before_events
            )

        with transaction(connection):
            job2 = repo.create_job(
                job_type="demo.job2",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
            running2 = repo.transition_job(
                job2.id,
                expected_status=JobStatus.PENDING,
                expected_version=1,
                new_status=JobStatus.RUNNING,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=5),
            )
            failed2 = repo.transition_job(
                running2.id,
                expected_status=JobStatus.RUNNING,
                expected_version=2,
                new_status=JobStatus.FAILED,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=6),
                last_error="once",
            )
            pending2 = repo.transition_job(
                failed2.id,
                expected_status=JobStatus.FAILED,
                expected_version=3,
                new_status=JobStatus.PENDING,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=7),
                retry=True,
            )
            assert pending2.status is JobStatus.PENDING
            assert pending2.attempts == 1
            assert pending2.last_error == "once"
            assert pending2.finished_at is None

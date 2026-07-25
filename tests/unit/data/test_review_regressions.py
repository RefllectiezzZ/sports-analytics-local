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
    apply_migrations,
    compute_migration_checksum,
    ensure_database_ready,
    get_migration_status,
    split_sql_statements,
    validate_migration_sequence,
    validate_migration_sql,
)
from sports_analytics.data.repositories.application import ApplicationMetadataRepository
from sports_analytics.data.repositories.audit import AuditEventRepository
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.types import (
    MAX_DURATION_SECONDS,
    JobStatus,
    Migration,
    SnapshotStatus,
    parse_positive_decimal_int,
    validate_positive_duration_seconds,
    validate_positive_finite_number,
    validate_relative_snapshot_path,
    validate_strict_int,
)
from sports_analytics.jobs.types import WorkerStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
WORKER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _register_running_worker(connection) -> str:
    worker = WorkerRepository(connection).register_worker(
        worker_id=WORKER_ID,
        name="test-worker",
        process_id=1234,
        hostname="test-host",
        started_at=FIXED,
        heartbeat_at=FIXED,
        capabilities={"job_types": ["demo.job", "demo.job2"]},
        status=WorkerStatus.RUNNING,
    )
    return worker.id


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


def test_application_metadata_upsert_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = ApplicationMetadataRepository(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.upsert("k", "v", FIXED)
        assert repo.get("k") is None
        with transaction(connection):
            repo.upsert("k", "v", FIXED)
        assert repo.get("k") == "v"


def test_job_create_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.create_job(
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
        assert repo.count_jobs() == 0
        with transaction(connection):
            repo.create_job(
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
        assert repo.count_jobs() == 1


def test_job_transition_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = JobRepository(connection)
        with transaction(connection):
            job = repo.create_job(
                job_type="demo.job",
                payload={},
                maximum_attempts=2,
                actor="cli",
                created_at=FIXED,
            )
        events_before = connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"]
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.transition_job(
                job.id,
                expected_status=JobStatus.PENDING,
                expected_version=1,
                new_status=JobStatus.CANCELLED,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=1),
            )
        assert repo.get_job(job.id) == job
        assert (
            connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"]
            == events_before
        )
        with transaction(connection):
            cancelled = repo.transition_job(
                job.id,
                expected_status=JobStatus.PENDING,
                expected_version=1,
                new_status=JobStatus.CANCELLED,
                actor="worker",
                occurred_at=FIXED.replace(microsecond=1),
            )
        assert cancelled.status is JobStatus.CANCELLED
        assert (
            connection.execute("SELECT COUNT(*) AS c FROM job_events").fetchone()["c"]
            == events_before + 1
        )


def test_snapshot_create_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/file.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED,
            )
        assert repo.list_snapshots() == []
        with transaction(connection):
            repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/file.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED,
            )
        assert len(repo.list_snapshots()) == 1


def test_snapshot_mark_ready_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    checksum = "a" * 64
    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            building = repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/ready.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED,
            )
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.mark_snapshot_ready(
                building.id,
                checksum_sha256=checksum,
                row_count=1,
                expected_version=1,
                ready_at=FIXED.replace(microsecond=1),
            )
        assert repo.get_snapshot(building.id) == building
        with transaction(connection):
            ready = repo.mark_snapshot_ready(
                building.id,
                checksum_sha256=checksum,
                row_count=1,
                expected_version=1,
                ready_at=FIXED.replace(microsecond=1),
            )
        assert ready.status is SnapshotStatus.READY


def test_snapshot_mark_failed_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            building = repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/fail.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED,
            )
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.mark_snapshot_failed(building.id, expected_version=1)
        assert repo.get_snapshot(building.id) == building
        with transaction(connection):
            failed = repo.mark_snapshot_failed(building.id, expected_version=1)
        assert failed.status is SnapshotStatus.FAILED


def test_audit_append_requires_transaction(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = AuditEventRepository(connection)
        with pytest.raises(RepositoryError, match="active explicit transaction"):
            repo.append_event(
                event_type="demo.event",
                entity_type="demo",
                actor="cli",
                details={},
                occurred_at=FIXED,
            )
        assert repo.list_events() == []
        with transaction(connection):
            repo.append_event(
                event_type="demo.event",
                entity_type="demo",
                actor="cli",
                details={},
                occurred_at=FIXED,
            )
        assert len(repo.list_events()) == 1


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
                    new_status=JobStatus.CANCELLED,
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
            connection.execute("DELETE FROM schema_migrations WHERE version = 2")
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


def test_positive_finite_number_validation_rejects_bool_nan_and_infinity() -> None:
    assert validate_positive_finite_number(1, field_name="seconds") == 1.0
    assert validate_positive_finite_number(0.25, field_name="seconds") == 0.25
    for value in (True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "1"):
        with pytest.raises(RepositoryError, match="positive finite number"):
            validate_positive_finite_number(value, field_name="seconds")
    with pytest.raises(RepositoryError, match="positive finite number"):
        validate_positive_finite_number(10**10000, field_name="seconds")
    with pytest.raises(RepositoryError):
        validate_positive_duration_seconds(float("1e308"), field_name="seconds")
    with pytest.raises(RepositoryError, match="must be <="):
        validate_positive_duration_seconds(MAX_DURATION_SECONDS + 1, field_name="seconds")
    assert validate_positive_duration_seconds(1.5, field_name="seconds") == 1.5


def test_parse_positive_decimal_int_rejects_non_canonical_forms() -> None:
    assert parse_positive_decimal_int(100, field_name="batch") == 100
    assert parse_positive_decimal_int("100", field_name="batch") == 100
    for value in (True, False, 0, -1, 1.0, "1.0", "01", "+1", " 1", "1 ", "1e2", "", None, "0"):
        with pytest.raises(RepositoryError, match="positive integer"):
            parse_positive_decimal_int(value, field_name="batch")


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
        queue = JobQueueRepository(connection)
        with transaction(connection):
            _register_running_worker(connection)
            repo.create_job(
                job_type="demo.job",
                payload={},
                maximum_attempts=1,
                actor="cli",
                created_at=FIXED,
            )
            claim = queue.claim_next_job(
                worker_id=WORKER_ID,
                claimed_at=FIXED.replace(microsecond=1),
                lease_duration_seconds=60,
                actor="worker",
            )
            assert claim is not None
            with pytest.raises(RepositoryError, match="fail_claimed_job"):
                repo.transition_job(
                    claim.job.id,
                    expected_status=JobStatus.RUNNING,
                    expected_version=2,
                    new_status=JobStatus.PENDING,
                    actor="worker",
                    occurred_at=FIXED.replace(microsecond=2),
                )
            outcome = queue.fail_claimed_job(
                job_id=claim.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim.job.version,
                failed_at=FIXED.replace(microsecond=3),
                error="boom",
                retryable=True,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
            )
            failed = outcome.job
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
            claim2 = queue.claim_next_job(
                worker_id=WORKER_ID,
                claimed_at=FIXED.replace(microsecond=5),
                lease_duration_seconds=60,
                actor="worker",
            )
            assert claim2 is not None and claim2.job.id == job2.id
            outcome2 = queue.fail_claimed_job(
                job_id=claim2.job.id,
                worker_id=WORKER_ID,
                expected_job_version=claim2.job.version,
                failed_at=FIXED.replace(microsecond=6),
                error="once",
                retryable=False,
                actor="worker",
                retry_backoff_base_seconds=5,
                retry_backoff_max_seconds=300,
            )
            failed2 = outcome2.job
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


def _assert_rejected_existing_file_unchanged(path: Path) -> None:
    before = path.read_bytes()
    sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
    with pytest.raises(DatabaseConnectionError, match="non-SQLite") as exc_info:
        with connect_database(path):
            pass
    assert str(path) in str(exc_info.value)
    assert path.read_bytes() == before
    for sidecar in sidecars:
        assert not sidecar.exists()


def test_existing_zero_byte_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    _assert_rejected_existing_file_unchanged(path)


def test_existing_short_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "short.sqlite3"
    path.write_bytes(b"SQLite")
    _assert_rejected_existing_file_unchanged(path)


def test_existing_partial_sqlite_header_rejected(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite3"
    path.write_bytes(SQLITE_HEADER[:-1])
    _assert_rejected_existing_file_unchanged(path)


def test_existing_arbitrary_non_sqlite_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "arbitrary.sqlite3"
    path.write_bytes(b"this is not a sqlite database file at all")
    _assert_rejected_existing_file_unchanged(path)


def test_nonexistent_path_creates_new_database(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "new.sqlite3"
    assert not path.exists()
    with connect_database(path) as connection:
        connection.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        with transaction(connection):
            connection.execute("INSERT INTO demo DEFAULT VALUES")
    assert path.is_file()
    assert read_sqlite_header(path) == SQLITE_HEADER


def test_commit_failure_rolls_back_deferred_fk(tmp_path: Path) -> None:
    path = tmp_path / "ops.sqlite3"
    with connect_database(path) as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            """
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute("INSERT INTO child(id, parent_id) VALUES (1, 999)")
        assert not connection.in_transaction
        assert connection.execute("SELECT COUNT(*) AS c FROM child").fetchone()["c"] == 0
        with transaction(connection):
            connection.execute("INSERT INTO parent(id) VALUES (1)")
            connection.execute("INSERT INTO child(id, parent_id) VALUES (1, 1)")
        assert connection.execute("SELECT COUNT(*) AS c FROM child").fetchone()["c"] == 1


def test_migration_rejects_all_pragma_and_transaction_controls() -> None:
    prohibited = [
        "PRAGMA journal_mode=OFF;",
        "PRAGMA main.journal_mode=OFF;",
        "/* harmless comment */ PRAGMA writable_schema=ON;",
        "/* PRAGMA user_version */ PRAGMA writable_schema=ON;",
        "-- comment\nPRAGMA foreign_keys=OFF;",
        "END TRANSACTION;",
        "SAVEPOINT sp1;",
        "RELEASE sp1;",
        "BEGIN IMMEDIATE;",
        "COMMIT;",
        "ROLLBACK;",
        "VACUUM;",
        "ATTACH DATABASE 'x.db' AS other;",
        "DETACH DATABASE other;",
    ]
    for sql in prohibited:
        with pytest.raises(DatabaseMigrationError, match="prohibited"):
            validate_migration_sql(sql, filename="0002_x.sql")

    # PRAGMA only inside a string or comment must not reject the statement.
    validate_migration_sql(
        "CREATE TABLE demo(note TEXT DEFAULT 'PRAGMA journal_mode=OFF');",
        filename="0002_ok.sql",
    )
    validate_migration_sql(
        "/* PRAGMA journal_mode=OFF */ CREATE TABLE demo(id INTEGER);",
        filename="0002_ok.sql",
    )


def test_sql_splitter_supports_triggers_and_trailing_comments() -> None:
    trigger_sql = """
    CREATE TABLE example(id INTEGER PRIMARY KEY);
    CREATE TABLE counters(value INTEGER NOT NULL);
    CREATE TABLE audit_log(message TEXT NOT NULL);
    CREATE TRIGGER example_trigger
    AFTER INSERT ON example
    BEGIN
        UPDATE counters SET value = value + 1;
        INSERT INTO audit_log(message) VALUES ('inserted');
    END;
    CREATE TABLE after_trigger(id INTEGER PRIMARY KEY);
    """
    statements = split_sql_statements(trigger_sql)
    assert len(statements) == 5
    assert statements[3].upper().startswith("CREATE TRIGGER")
    assert "UPDATE COUNTERS" in statements[3].upper()
    assert statements[4].upper().startswith("CREATE TABLE AFTER_TRIGGER")

    assert split_sql_statements("CREATE TABLE a(id INTEGER); -- trailing") == [
        "CREATE TABLE a(id INTEGER)"
    ]
    assert split_sql_statements("CREATE TABLE a(id INTEGER);\n-- trailing\n") == [
        "CREATE TABLE a(id INTEGER)"
    ]
    assert split_sql_statements("CREATE TABLE a(id INTEGER); /* trailing */") == [
        "CREATE TABLE a(id INTEGER)"
    ]
    assert split_sql_statements("-- only comments\n/* still comments */") == []
    assert split_sql_statements("CREATE TABLE a(id INTEGER)") == ["CREATE TABLE a(id INTEGER)"]
    with pytest.raises(DatabaseMigrationError, match="incomplete"):
        split_sql_statements("CREATE TABLE a(")
    assert split_sql_statements("CREATE TABLE a(note TEXT DEFAULT 'a;b');") == [
        "CREATE TABLE a(note TEXT DEFAULT 'a;b')"
    ]
    assert split_sql_statements('CREATE TABLE "weird;name"(id INTEGER);') == [
        'CREATE TABLE "weird;name"(id INTEGER)'
    ]
    assert split_sql_statements("CREATE TABLE [bracket;name](id INTEGER);") == [
        "CREATE TABLE [bracket;name](id INTEGER)"
    ]
    assert split_sql_statements("CREATE TABLE `tick;name`(id INTEGER);") == [
        "CREATE TABLE `tick;name`(id INTEGER)"
    ]
    with pytest.raises(DatabaseMigrationError, match="unclosed"):
        split_sql_statements("CREATE TABLE a(note TEXT DEFAULT 'oops);")
    with pytest.raises(DatabaseMigrationError, match="unclosed block comment"):
        split_sql_statements("CREATE TABLE a(id INTEGER); /* forever")


def test_discover_migrations_wraps_resource_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from sports_analytics.data import migrations as migrations_module

    class _BoomRoot:
        def iterdir(self):
            raise OSError("iterdir failed")

    monkeypatch.setattr(
        migrations_module.resources,
        "files",
        lambda package: _BoomRoot(),
    )
    with pytest.raises(DatabaseMigrationError, match="enumerating|failed") as exc_info:
        migrations_module.discover_migrations()
    assert isinstance(exc_info.value.__cause__, OSError)

    class _BadRead:
        name = "0001_initial.sql"

        def is_file(self) -> bool:
            return True

        def read_text(self, encoding: str = "utf-8") -> str:
            del encoding
            raise OSError("read failed")

    class _Root:
        def __init__(self, files: list[object]) -> None:
            self._files = files

        def iterdir(self):
            return iter(self._files)

    monkeypatch.setattr(
        migrations_module.resources,
        "files",
        lambda package: _Root([_BadRead()]),
    )
    with pytest.raises(DatabaseMigrationError, match="reading migration") as read_exc:
        migrations_module.discover_migrations()
    assert isinstance(read_exc.value.__cause__, OSError)

    class _BadUtf8:
        name = "0001_initial.sql"

        def is_file(self) -> bool:
            return True

        def read_text(self, encoding: str = "utf-8") -> str:
            del encoding
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    monkeypatch.setattr(
        migrations_module.resources,
        "files",
        lambda package: _Root([_BadUtf8()]),
    )
    with pytest.raises(DatabaseMigrationError, match="UTF-8|reading migration") as utf_exc:
        migrations_module.discover_migrations()
    assert isinstance(utf_exc.value.__cause__, UnicodeError)


def test_sql_comments_preserve_lexical_whitespace() -> None:
    statements = split_sql_statements("CREATE/* comment */TABLE demo(id INTEGER);")
    assert len(statements) == 1
    assert " ".join(statements[0].split()).upper().startswith("CREATE TABLE")

    statements = split_sql_statements("CREATE TABLE demo(value INT/* comment */NOT NULL);")
    assert len(statements) == 1
    compact = " ".join(statements[0].split()).upper()
    assert "INT NOT NULL" in compact

    statements = split_sql_statements("CREATE/* x */INDEX demo_idx ON demo(id);")
    assert " ".join(statements[0].split()).upper().startswith("CREATE INDEX")

    statements = split_sql_statements("SELECT 1/* plus */+/* two */2;")
    assert len(statements) == 1
    assert "+" in statements[0]

    statements = split_sql_statements("/* leading */ CREATE TABLE demo(id INTEGER);")
    assert len(statements) == 1
    assert " ".join(statements[0].split()).upper().startswith("CREATE TABLE")

    statements = split_sql_statements(
        "CREATE TABLE a(id INTEGER); /* between; PRAGMA BEGIN COMMIT */ CREATE TABLE b(id INTEGER);"
    )
    assert len(statements) == 2

    assert split_sql_statements("CREATE TABLE a(id INTEGER); -- trailing BEGIN") == [
        "CREATE TABLE a(id INTEGER)"
    ]
    assert split_sql_statements("CREATE TABLE a(id INTEGER); /* trailing COMMIT; */") == [
        "CREATE TABLE a(id INTEGER)"
    ]
    assert split_sql_statements("/* only PRAGMA BEGIN COMMIT ; */") == []
    assert split_sql_statements("CREATE TABLE demo(note TEXT DEFAULT '/* not a comment */');") == [
        "CREATE TABLE demo(note TEXT DEFAULT '/* not a comment */')"
    ]

    validate_migration_sql(
        "/* PRAGMA journal_mode=OFF */ CREATE TABLE demo(id INTEGER);",
        filename="0002_ok.sql",
    )
    with pytest.raises(DatabaseMigrationError, match="prohibited"):
        validate_migration_sql(
            "CREATE TABLE demo(id INTEGER); PRAGMA journal_mode=OFF;",
            filename="0002_x.sql",
        )


def test_comment_preserving_migration_enforces_not_null(tmp_path: Path) -> None:
    sql = """
    CREATE TABLE demo(
        value INT/* required */NOT NULL
    );
    """
    checksum = compute_migration_checksum(sql)
    migration = Migration(
        version=1,
        name="demo",
        sql_text=sql,
        checksum=checksum,
        filename="0001_demo.sql",
    )
    assert migration.checksum == compute_migration_checksum(migration.sql_text)
    db = tmp_path / "demo.sqlite3"
    apply_migrations(db, migrations=(migration,))
    with connect_database(db, read_only=True) as connection:
        columns = connection.execute("PRAGMA table_info(demo)").fetchall()
        assert len(columns) == 1
        assert str(columns[0]["name"]) == "value"
        assert int(columns[0]["notnull"]) == 1
    with connect_database(db) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO demo(value) VALUES (NULL)")


def test_validate_migration_sequence_types_before_sort() -> None:
    sql = "CREATE TABLE demo(id INTEGER PRIMARY KEY);"
    checksum = compute_migration_checksum(sql)

    def _migration(**overrides: object) -> Migration:
        values: dict[str, object] = {
            "version": 1,
            "name": "demo",
            "sql_text": sql,
            "checksum": checksum,
            "filename": "0001_demo.sql",
        }
        values.update(overrides)
        return Migration(**values)  # type: ignore[arg-type]

    second = Migration(
        version=2,
        name="second",
        sql_text=sql,
        checksum=checksum,
        filename="0002_second.sql",
    )
    first = _migration()
    ordered = validate_migration_sequence((second, first))
    assert [item.version for item in ordered] == [1, 2]

    cases: list[object] = [
        (_migration(version="1"), "version"),
        (_migration(version=True), "version"),
        (_migration(version=1.0), "version"),
        ("not-a-migration", "Migration"),
        (_migration(name=123), "name"),
        (_migration(filename=123), "filename"),
        (_migration(checksum=123), "checksum"),
        (_migration(sql_text=b"CREATE TABLE demo(id INTEGER);"), "sql_text"),
        (_migration(checksum="ZZ"), "checksum"),
        ((first, first), "duplicate"),
        (
            (
                first,
                Migration(
                    version=3,
                    name="gap",
                    sql_text=sql,
                    checksum=checksum,
                    filename="0003_gap.sql",
                ),
            ),
            "consecutive",
        ),
    ]
    for payload, _label in cases:
        with pytest.raises(DatabaseMigrationError) as exc_info:
            if isinstance(payload, tuple):
                validate_migration_sequence(payload)
            else:
                validate_migration_sequence((payload,))  # type: ignore[arg-type]
        assert type(exc_info.value) is DatabaseMigrationError


class _FlakyConnection(sqlite3.Connection):
    """Connection subclass that can fail rollback/close on demand."""

    fail_rollback = False
    fail_close = False

    def rollback(self) -> None:  # type: ignore[override]
        if self.fail_rollback:
            raise RuntimeError("rollback cleanup failed")
        super().rollback()

    def close(self) -> None:  # type: ignore[override]
        if self.fail_close:
            raise RuntimeError("close cleanup failed")
        super().close()


def _patch_sqlite_connect_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    original_connect = sqlite3.connect

    def _factory(*args: object, **kwargs: object) -> _FlakyConnection:
        kwargs = dict(kwargs)
        kwargs["factory"] = _FlakyConnection
        return original_connect(*args, **kwargs)  # type: ignore[misc]

    monkeypatch.setattr(sqlite3, "connect", _factory)


def test_transaction_preserves_caller_exception_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ops.sqlite3"
    _patch_sqlite_connect_factory(monkeypatch)

    class CallerBoom(RuntimeError):
        pass

    with connect_database(path) as connection:
        assert isinstance(connection, _FlakyConnection)
        connection.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        connection.fail_rollback = True
        with pytest.raises(CallerBoom, match="caller body failed") as exc_info:
            with transaction(connection):
                connection.execute("INSERT INTO demo DEFAULT VALUES")
                raise CallerBoom("caller body failed")
        assert type(exc_info.value) is CallerBoom
        assert "rollback cleanup failed" not in str(exc_info.value)


def test_transaction_preserves_commit_exception_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ops.sqlite3"
    _patch_sqlite_connect_factory(monkeypatch)

    class CommitBoom(RuntimeError):
        pass

    with connect_database(path) as connection:
        assert isinstance(connection, _FlakyConnection)
        connection.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY)")
        original_commit = connection.commit

        def _boom_commit() -> None:
            raise CommitBoom("commit failed")

        connection.commit = _boom_commit  # type: ignore[method-assign]
        connection.fail_rollback = True
        with pytest.raises(CommitBoom, match="commit failed") as exc_info:
            with transaction(connection):
                connection.execute("INSERT INTO demo DEFAULT VALUES")
        assert type(exc_info.value) is CommitBoom
        connection.commit = original_commit  # type: ignore[method-assign]


def test_connect_database_preserves_caller_exception_when_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ops.sqlite3"
    _patch_sqlite_connect_factory(monkeypatch)

    class CallerBoom(RuntimeError):
        pass

    with pytest.raises(CallerBoom, match="caller body failed") as exc_info:
        with connect_database(path) as connection:
            assert isinstance(connection, _FlakyConnection)
            connection.fail_close = True
            raise CallerBoom("caller body failed")
    assert type(exc_info.value) is CallerBoom


def test_connect_database_propagates_close_failure_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ops.sqlite3"
    _patch_sqlite_connect_factory(monkeypatch)

    with pytest.raises(RuntimeError, match="close cleanup failed"):
        with connect_database(path) as connection:
            assert isinstance(connection, _FlakyConnection)
            connection.fail_close = True
            connection.execute("SELECT 1")

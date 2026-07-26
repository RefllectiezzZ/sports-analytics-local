"""Migration 0005 bookmaker acquisition offline upgrade coverage."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import (
    apply_migrations,
    discover_migrations,
    ensure_database_ready,
)

EXPECTED_PREVIOUS = {
    1: "404e1c0b36390ff7a42de901f344edcb60b9cee248b741116bc9d47a17cf48de",
    2: "94af0d6d9df740ac0c578c815015fe3981acfc48f5faa3cfb1ba3bc1a719b55d",
    3: "84fda02807a42e9e951d4fad4e8bedeecd1a2fda675be929762394ac5cc2ec94",
    4: "8559eecc1565808578ab402250481e94f15d31a49b7714ff43c4b413702ef11d",
}
MIGRATION_0005_CHECKSUM = "05ab73e8d6fb2489ed21ee82eaa2b6b84c428d27b69beae593d2efa20b2d73ff"


def test_migration_0005_checksum_pinned_from_discover() -> None:
    migrations = discover_migrations()
    assert [item.version for item in migrations] == [1, 2, 3, 4, 5]
    assert {item.version: item.checksum for item in migrations[:4]} == EXPECTED_PREVIOUS
    assert migrations[4].filename == "0005_bookmaker_acquisition.sql"
    assert migrations[4].checksum == MIGRATION_0005_CHECKSUM
    assert migrations[4].checksum == hashlib.sha256(migrations[4].sql_text.encode()).hexdigest()


def test_empty_database_upgrades_to_5_and_repeated_is_noop(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    first = ensure_database_ready(database)
    second = ensure_database_ready(database)
    assert first.schema_version == second.schema_version == 5
    assert second.migrations_applied == ()
    with connect_database(database, read_only=True) as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "bookmaker_acquisition_runs",
        "bookmaker_acquisition_attempts",
        "bookmaker_provider_status",
        "bookmaker_snapshot_registrations",
        "bookmaker_scheduler_anchors",
        "bookmaker_scheduler_cycles",
        "bookmaker_fallback_decisions",
        "bookmaker_drift_findings",
    } <= tables


def test_schema_version_4_upgrades_to_5(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    migrations = discover_migrations()
    version_4 = apply_migrations(database, migrations=migrations[:4])
    upgraded = apply_migrations(database, migrations=migrations)
    assert version_4.schema_version == 4
    assert upgraded.previous_version == 4
    assert upgraded.schema_version == 5
    assert [item.version for item in upgraded.migrations_applied] == [5]
    assert upgraded.migrations_applied[0].checksum == MIGRATION_0005_CHECKSUM


def test_foreign_keys_and_unique_constraints(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO bookmaker_acquisition_runs (
                    id, provider_id, sport, acquisition_cycle_id, adapter_version,
                    status, observed_at, started_at, finished_at, snapshot_id,
                    block_reason, failure_classification, warnings_json
                ) VALUES (
                    'run-1', 'betano-pt', 'football', 'cycle-1', 'betano-pt-adapter-v1',
                    'succeeded', '2026-07-26T12:00:00.000000Z',
                    '2026-07-26T12:00:00.000000Z', '2026-07-26T12:00:01.000000Z',
                    NULL, NULL, 'none', '[]'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO bookmaker_acquisition_runs (
                        id, provider_id, sport, acquisition_cycle_id, adapter_version,
                        status, observed_at, started_at, finished_at, snapshot_id,
                        block_reason, failure_classification, warnings_json
                    ) VALUES (
                        'run-2', 'betano-pt', 'football', 'cycle-1', 'betano-pt-adapter-v1',
                        'succeeded', '2026-07-26T12:00:00.000000Z',
                        '2026-07-26T12:00:00.000000Z', '2026-07-26T12:00:01.000000Z',
                        NULL, NULL, 'none', '[]'
                    )
                    """
                )
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO bookmaker_acquisition_attempts (
                        id, run_id, attempt_number, started_at, finished_at, outcome,
                        failure_classification, detail_code
                    ) VALUES (
                        'attempt-1', 'missing-run', 1,
                        '2026-07-26T12:00:00.000000Z', '2026-07-26T12:00:01.000000Z',
                        'failed', 'permanent', 'boom'
                    )
                    """
                )
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO jobs (
                    id, job_type, payload_json, status, priority, attempts,
                    maximum_attempts, available_at, created_at, updated_at, version
                ) VALUES (
                    'job-1', 'ingest.bookmaker-current-odds', '{}', 'pending', 0, 0,
                    2, '2026-07-26T12:00:00.000000Z',
                    '2026-07-26T12:00:00.000000Z', '2026-07-26T12:00:00.000000Z', 1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO bookmaker_scheduler_cycles (
                    id, provider_id, sport, scheduled_for, enqueued_at, job_id,
                    suppressed_duplicate
                ) VALUES (
                    'cycle-row-1', 'betano-pt', 'football',
                    '2026-07-26T12:00:00.000000Z', '2026-07-26T12:00:00.000000Z',
                    'job-1', 0
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO bookmaker_scheduler_cycles (
                        id, provider_id, sport, scheduled_for, enqueued_at, job_id,
                        suppressed_duplicate
                    ) VALUES (
                        'cycle-row-2', 'betano-pt', 'football',
                        '2026-07-26T12:00:00.000000Z', '2026-07-26T12:00:00.000000Z',
                        'job-2', 0
                    )
                    """
                )

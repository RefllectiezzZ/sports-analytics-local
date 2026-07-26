"""Focused tests for migration 0003 snapshot source-version deduplication."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseIntegrityError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import (
    apply_migrations,
    compute_migration_checksum,
    discover_migrations,
    ensure_database_ready,
)
from sports_analytics.data.repositories.snapshots import SnapshotRepository

CHECKSUM_0001 = "404e1c0b36390ff7a42de901f344edcb60b9cee248b741116bc9d47a17cf48de"
CHECKSUM_0002 = "94af0d6d9df740ac0c578c815015fe3981acfc48f5faa3cfb1ba3bc1a719b55d"
CHECKSUM_0003 = "84fda02807a42e9e951d4fad4e8bedeecd1a2fda675be929762394ac5cc2ec94"
READY_CHECKSUM = "a" * 64


def test_migration_0003_discovered_with_expected_checksum() -> None:
    migrations = discover_migrations()

    assert [migration.version for migration in migrations] == [1, 2, 3, 4, 5]
    assert migrations[0].checksum == CHECKSUM_0001
    assert migrations[1].checksum == CHECKSUM_0002
    assert migrations[2].filename == "0003_snapshot_source_deduplication.sql"
    assert migrations[2].name == "snapshot_source_deduplication"
    assert migrations[2].checksum == CHECKSUM_0003

    text = (
        resources.files("sports_analytics.data.sql.migrations")
        .joinpath("0003_snapshot_source_deduplication.sql")
        .read_text(encoding="utf-8")
    )
    assert compute_migration_checksum(text) == CHECKSUM_0003


def test_fresh_migration_reaches_version_3(tmp_path: Path) -> None:
    readiness = ensure_database_ready(tmp_path / "ops.sqlite3")

    assert readiness.previous_version == 0
    assert readiness.schema_version == 5
    assert [migration.version for migration in readiness.migrations_applied] == [1, 2, 3, 4, 5]


def test_version_2_database_upgrades_to_version_3(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    migrations = discover_migrations()

    version_2 = apply_migrations(db, migrations=migrations[:2])
    assert version_2.schema_version == 2

    upgraded = apply_migrations(db, migrations=migrations)
    assert upgraded.previous_version == 2
    assert upgraded.schema_version == 5
    assert [migration.version for migration in upgraded.migrations_applied] == [3, 4, 5]

    with connect_database(db, read_only=True) as connection:
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "uq_snapshots_active_source_version" in indexes
    assert "idx_snapshots_source_version_status" in indexes


def test_migration_0003_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    first = ensure_database_ready(db)
    second = ensure_database_ready(db)

    assert first.schema_version == 5
    assert second.previous_version == 5
    assert second.schema_version == 5
    assert second.migrations_applied == ()


@pytest.mark.parametrize("existing_status", ["building", "ready"])
def test_unique_partial_index_rejects_two_active_same_source_version(
    tmp_path: Path,
    existing_status: str,
) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)

    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            first = repo.create_building_snapshot(
                snapshot_id="11111111-1111-4111-8111-111111111111",
                snapshot_type="football-canonical",
                relative_path="snapshots/first.parquet",
                source_name="football-data-co-uk",
                source_version="e0-2324-v1",
                schema_version="football-canonical-v1",
                metadata={"fixture": "first"},
            )
            if existing_status == "ready":
                repo.mark_snapshot_ready(
                    first.id,
                    checksum_sha256=READY_CHECKSUM,
                    row_count=1,
                    expected_version=first.version,
                )

        with pytest.raises(DatabaseIntegrityError, match="integrity error creating snapshot"):
            with transaction(connection):
                repo.create_building_snapshot(
                    snapshot_id="22222222-2222-4222-8222-222222222222",
                    snapshot_type="football-canonical",
                    relative_path="snapshots/second.parquet",
                    source_name="football-data-co-uk",
                    source_version="e0-2324-v1",
                    schema_version="football-canonical-v1",
                    metadata={"fixture": "second"},
                )


def test_failed_snapshot_does_not_block_replacement(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)

    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            failed = repo.create_building_snapshot(
                snapshot_id="33333333-3333-4333-8333-333333333333",
                snapshot_type="football-canonical",
                relative_path="snapshots/failed.parquet",
                source_name="football-data-co-uk",
                source_version="e0-2324-v1",
                schema_version="football-canonical-v1",
                metadata={"attempt": 1},
            )
            repo.mark_snapshot_failed(
                failed.id,
                expected_version=failed.version,
                metadata={"attempt": 1, "error": "synthetic failure"},
            )

        with transaction(connection):
            replacement = repo.create_building_snapshot(
                snapshot_id="44444444-4444-4444-8444-444444444444",
                snapshot_type="football-canonical",
                relative_path="snapshots/replacement.parquet",
                source_name="football-data-co-uk",
                source_version="e0-2324-v1",
                schema_version="football-canonical-v1",
                metadata={"attempt": 2},
            )

    assert replacement.source_version == "e0-2324-v1"


def test_different_source_versions_and_schema_versions_are_allowed(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)

    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            records = [
                repo.create_building_snapshot(
                    snapshot_id="55555555-5555-4555-8555-555555555555",
                    snapshot_type="football-canonical",
                    relative_path="snapshots/source-v1.parquet",
                    source_name="football-data-co-uk",
                    source_version="e0-2324-v1",
                    schema_version="football-canonical-v1",
                    metadata={},
                ),
                repo.create_building_snapshot(
                    snapshot_id="66666666-6666-4666-8666-666666666666",
                    snapshot_type="football-canonical",
                    relative_path="snapshots/source-v2.parquet",
                    source_name="football-data-co-uk",
                    source_version="e0-2324-v2",
                    schema_version="football-canonical-v1",
                    metadata={},
                ),
                repo.create_building_snapshot(
                    snapshot_id="77777777-7777-4777-8777-777777777777",
                    snapshot_type="football-canonical",
                    relative_path="snapshots/schema-v2.parquet",
                    source_name="football-data-co-uk",
                    source_version="e0-2324-v1",
                    schema_version="football-canonical-v2",
                    metadata={},
                ),
            ]

    assert [record.source_version for record in records] == [
        "e0-2324-v1",
        "e0-2324-v2",
        "e0-2324-v1",
    ]


def test_package_discovery_is_cwd_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    migrations = discover_migrations()

    assert [migration.version for migration in migrations] == [1, 2, 3, 4, 5]
    assert migrations[2].checksum == CHECKSUM_0003


def test_sqlite_partial_index_predicate_excludes_failed_rows(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)

    with connect_database(db) as connection, transaction(connection):
        connection.execute(
            """
            INSERT INTO snapshots (
                id, snapshot_type, status, relative_path, checksum_sha256,
                row_count, source_name, source_version, schema_version,
                created_at, ready_at, metadata_json, version
            ) VALUES (
                '88888888-8888-4888-8888-888888888888',
                'football-canonical',
                'failed',
                'snapshots/direct-failed.parquet',
                NULL,
                NULL,
                'football-data-co-uk',
                'e0-2324-v1',
                'football-canonical-v1',
                '2025-01-01T00:00:00.000000Z',
                NULL,
                '{}',
                1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO snapshots (
                id, snapshot_type, status, relative_path, checksum_sha256,
                row_count, source_name, source_version, schema_version,
                created_at, ready_at, metadata_json, version
            ) VALUES (
                '99999999-9999-4999-8999-999999999999',
                'football-canonical',
                'building',
                'snapshots/direct-building.parquet',
                NULL,
                NULL,
                'football-data-co-uk',
                'e0-2324-v1',
                'football-canonical-v1',
                '2025-01-01T00:00:01.000000Z',
                NULL,
                '{}',
                1
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO snapshots (
                    id, snapshot_type, status, relative_path, checksum_sha256,
                    row_count, source_name, source_version, schema_version,
                    created_at, ready_at, metadata_json, version
                ) VALUES (
                    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                    'football-canonical',
                    'ready',
                    'snapshots/direct-ready.parquet',
                    ?,
                    1,
                    'football-data-co-uk',
                    'e0-2324-v1',
                    'football-canonical-v1',
                    '2025-01-01T00:00:02.000000Z',
                    '2025-01-01T00:00:03.000000Z',
                    '{}',
                    1
                )
                """,
                (READY_CHECKSUM,),
            )

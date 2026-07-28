from __future__ import annotations

import hashlib
import sqlite3

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
}


def test_migration_0004_is_next_and_previous_checksums_unchanged() -> None:
    migrations = discover_migrations()
    assert [item.version for item in migrations] == [1, 2, 3, 4, 5]
    assert {item.version: item.checksum for item in migrations[:3]} == EXPECTED_PREVIOUS
    assert migrations[3].filename == "0004_settlement_monitoring_governance.sql"
    assert migrations[3].checksum == hashlib.sha256(migrations[3].sql_text.encode()).hexdigest()
    assert migrations[4].filename == "0005_bookmaker_acquisition.sql"
    assert migrations[4].checksum == hashlib.sha256(migrations[4].sql_text.encode()).hexdigest()


def test_empty_database_upgrade_and_repeated_application(tmp_path) -> None:
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
        "result_snapshots",
        "settlement_runs",
        "analytical_settlements",
        "monitoring_runs",
        "model_registry_entries",
        "promotion_decisions",
        "model_role_transitions",
        "bookmaker_acquisition_runs",
        "bookmaker_provider_status",
        "bookmaker_snapshot_registrations",
        "bookmaker_scheduler_cycles",
    } <= tables


def test_pre_pr_schema_version_3_upgrades_through_bookmaker_acquisition(tmp_path) -> None:
    database = tmp_path / "operational.sqlite3"
    migrations = discover_migrations()
    version_3 = apply_migrations(database, migrations=migrations[:3])
    upgraded = apply_migrations(database, migrations=migrations)
    assert version_3.schema_version == 3
    assert upgraded.previous_version == 3
    assert upgraded.schema_version == 5
    assert [item.version for item in upgraded.migrations_applied] == [4, 5]


def test_foreign_keys_and_one_champion_scope(tmp_path) -> None:
    database = tmp_path / "operational.sqlite3"
    ensure_database_ready(database)
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO settlement_evidence (
                        settlement_id, opportunity_id, canonical_event_id,
                        result_snapshot_id, result_checksum_sha256
                    ) VALUES ('missing', 'op', 'event', 'missing', ?)
                    """,
                    ("a" * 64,),
                )
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO model_registry_entries (
                    model_artifact_id, model_checksum_sha256, model_relative_path,
                    model_specification_version, feature_specification_version,
                    sport_code, market_key, role, lifecycle_status, registered_at,
                    actor, provenance_json, version
                ) VALUES ('champion-1', ?, 'champion-1/model.json',
                          'model-v1', 'features-v1', 'football',
                          'football:match', 'champion', 'promoted',
                          '2026-01-01T00:00:00.000000Z', 'test', '{}', 1)
                """,
                ("a" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO model_registry_entries (
                        model_artifact_id, model_checksum_sha256, model_relative_path,
                        model_specification_version, feature_specification_version,
                        sport_code, market_key, role, lifecycle_status, registered_at,
                        actor, provenance_json, version
                    ) VALUES ('champion-2', ?, 'champion-2/model.json',
                              'model-v1', 'features-v1', 'football',
                              'football:match', 'champion', 'promoted',
                              '2026-01-01T00:00:00.000000Z', 'test', '{}', 1)
                    """,
                    ("b" * 64,),
                )

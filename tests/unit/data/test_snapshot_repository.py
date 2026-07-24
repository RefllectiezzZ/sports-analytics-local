"""Tests for snapshot metadata repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)
SNAP_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CHECKSUM = "a" * 64


def test_snapshot_lifecycle_and_immutability(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    physical = tmp_path / "raw" / "data.parquet"
    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            building = repo.create_building_snapshot(
                snapshot_id=SNAP_ID,
                snapshot_type="raw.events",
                relative_path="raw/2026/data.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={"b": 1, "a": 2},
                created_at=FIXED,
            )
        assert building.status is SnapshotStatus.BUILDING
        assert dumps_canonical_json(building.metadata) == '{"a":2,"b":1}'
        assert not physical.exists()

        with pytest.raises(RepositoryError):
            with transaction(connection):
                repo.create_building_snapshot(
                    snapshot_type="raw.events",
                    relative_path="/abs/path.parquet",
                    source_name="local.fixture",
                    schema_version="v1",
                    metadata={},
                    created_at=FIXED,
                )
        with pytest.raises(RepositoryError):
            with transaction(connection):
                repo.create_building_snapshot(
                    snapshot_type="raw.events",
                    relative_path="../escape.parquet",
                    source_name="local.fixture",
                    schema_version="v1",
                    metadata={},
                    created_at=FIXED,
                )

        with transaction(connection):
            ready = repo.mark_snapshot_ready(
                SNAP_ID,
                checksum_sha256=CHECKSUM,
                row_count=10,
                expected_version=1,
                ready_at=FIXED.replace(microsecond=1),
            )
        assert ready.status is SnapshotStatus.READY
        assert ready.version == 2
        assert not physical.exists()

        with pytest.raises(RepositoryError, match="immutable"):
            with transaction(connection):
                repo.mark_snapshot_ready(
                    SNAP_ID,
                    checksum_sha256=CHECKSUM,
                    row_count=11,
                    expected_version=2,
                    ready_at=FIXED,
                )
        with pytest.raises(RepositoryError, match="immutable"):
            with transaction(connection):
                repo.mark_snapshot_failed(SNAP_ID, expected_version=2)


def test_snapshot_failed_and_listing(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = SnapshotRepository(connection)
        with transaction(connection):
            one = repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/one.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED,
            )
            two = repo.create_building_snapshot(
                snapshot_type="raw.events",
                relative_path="raw/two.parquet",
                source_name="local.fixture",
                schema_version="v1",
                metadata={},
                created_at=FIXED.replace(microsecond=1),
            )
            failed = repo.mark_snapshot_failed(
                one.id,
                expected_version=1,
                metadata={"error": "failed"},
            )
        assert failed.status is SnapshotStatus.FAILED
        assert failed.ready_at is None
        listed = repo.list_snapshots(status=SnapshotStatus.BUILDING)
        assert [item.id for item in listed] == [two.id]
        with pytest.raises(DatabaseIntegrityError):
            with transaction(connection):
                repo.mark_snapshot_ready(
                    two.id,
                    checksum_sha256=CHECKSUM,
                    row_count=1,
                    expected_version=99,
                    ready_at=FIXED,
                )
        with pytest.raises(RepositoryError):
            with transaction(connection):
                repo.mark_snapshot_ready(
                    two.id,
                    checksum_sha256="zz",
                    row_count=1,
                    expected_version=1,
                    ready_at=FIXED,
                )
        with pytest.raises(RepositoryError):
            with transaction(connection):
                repo.mark_snapshot_ready(
                    two.id,
                    checksum_sha256=CHECKSUM,
                    row_count=-1,
                    expected_version=1,
                    ready_at=FIXED,
                )

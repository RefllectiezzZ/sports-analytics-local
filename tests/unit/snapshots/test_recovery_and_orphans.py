"""Regression tests for BUILDING recovery, orphan adoption, and TX boundaries.

Orphan discovery is bounded to the identity's partition parent directory, so these
tests drive the sport-agnostic publication service through the football dataset
suite and assert that only an exact identity match is ever adopted.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from sports_analytics.core.exceptions import SnapshotBusyError, SnapshotIntegrityError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JsonValue, SnapshotRecord, SnapshotStatus
from sports_analytics.snapshots.manifest import write_manifest
from sports_analytics.snapshots.reader import (
    SnapshotVerificationResult,
    manifest_document_for,
    verify_snapshot_directory,
)
from sports_analytics.snapshots.spec import MANIFEST_FILENAME, SnapshotDatasetSuite
from sports_analytics.snapshots.writer import (
    PreparedSnapshot,
    prepare_snapshot_directory,
    resolve_snapshot_directory,
)
from sports_analytics.sports.football.schemas import football_snapshot_suite
from tests.helpers_snapshots import (
    build_spec,
    build_tables,
    database_path,
    prepare,
    publication_service,
)

UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UUID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

ALTERNATE_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,13/08/2023,Northbridge FC,Southport Athletic,1,0,H\n"
)
PRIMEIRA_LIGA_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"P1,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)

FOREIGN_RAW_SHA256 = "0" * 63 + "1"

OrphanFactory = Callable[[Path, Path], Path]


def _records(database: Path) -> list[SnapshotRecord]:
    """Return every snapshot metadata row, closing the connection before assertions."""
    with connect_database(database, read_only=True) as connection:
        return SnapshotRepository(connection).list_snapshots()


def _create_building_row(database: Path, prepared: PreparedSnapshot) -> SnapshotRecord:
    """Insert the BUILDING row a crashed publication would have left behind."""
    metadata: dict[str, JsonValue] = dict(prepared.domain_metadata)
    for key, value in prepared.partition_keys:
        metadata[key] = value
    with connect_database(database) as connection:
        with transaction(connection, immediate=True):
            return SnapshotRepository(connection).create_building_snapshot(
                snapshot_type=prepared.snapshot_type,
                relative_path=prepared.relative_manifest_path,
                source_name=prepared.source_name,
                schema_version=prepared.schema_version,
                metadata=metadata,
                snapshot_id=prepared.snapshot_id,
                source_version=prepared.source_version,
                created_at=prepared.source_observed_at_utc,
            )


def _promote_to_final(snapshots_directory: Path, prepared: PreparedSnapshot) -> Path:
    """Move a prepared temporary directory to its final immutable location."""
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared.temporary_directory.rename(final_directory)
    return final_directory


def _orphan(tmp_path: Path, snapshots_directory: Path, *, snapshot_id: str = UUID_A) -> Path:
    """Leave a complete default snapshot directory on disk with no metadata row."""
    return _promote_to_final(
        snapshots_directory,
        prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=snapshot_id),
    )


def _different_competition_orphan(tmp_path: Path, snapshots_directory: Path) -> Path:
    return _promote_to_final(
        snapshots_directory,
        prepare(
            tmp_path,
            snapshots_directory=snapshots_directory,
            snapshot_id=UUID_A,
            competition_id="prt-primeira-liga",
            content=PRIMEIRA_LIGA_CSV,
        ),
    )


def _different_season_orphan(tmp_path: Path, snapshots_directory: Path) -> Path:
    return _promote_to_final(
        snapshots_directory,
        prepare(
            tmp_path,
            snapshots_directory=snapshots_directory,
            snapshot_id=UUID_A,
            season_label="2022-2023",
            source_season_code="2223",
        ),
    )


def _different_source_version_orphan(tmp_path: Path, snapshots_directory: Path) -> Path:
    return _promote_to_final(
        snapshots_directory,
        prepare(
            tmp_path,
            snapshots_directory=snapshots_directory,
            snapshot_id=UUID_A,
            content=ALTERNATE_CSV,
        ),
    )


def _different_raw_checksum_orphan(tmp_path: Path, snapshots_directory: Path) -> Path:
    """Publish a verifiable orphan whose manifest points at a foreign raw artifact."""
    directory = _orphan(tmp_path, snapshots_directory)
    relative_manifest = (
        f"{directory.relative_to(snapshots_directory.resolve()).as_posix()}/{MANIFEST_FILENAME}"
    )
    document = manifest_document_for(
        snapshots_directory,
        relative_manifest,
        suite=football_snapshot_suite(),
    )
    raw_artifact = document["raw_artifact"]
    assert isinstance(raw_artifact, dict)
    raw_artifact["checksum_sha256"] = FOREIGN_RAW_SHA256
    write_manifest(directory / MANIFEST_FILENAME, document)
    return directory


def _different_schema_version_orphan(tmp_path: Path, snapshots_directory: Path) -> Path:
    """Publish an otherwise identical snapshot under a superseded schema version."""
    spec, bundle = build_spec(tmp_path, raw_subdirectory=f"raw-{UUID_A}")
    identity = replace(spec.identity, schema_version="football-canonical-v1")
    prepared = prepare_snapshot_directory(
        snapshots_directory=snapshots_directory,
        spec=replace(spec, identity=identity),
        tables=build_tables(bundle),
        snapshot_id=UUID_A,
    )
    return _promote_to_final(snapshots_directory, prepared)


def test_building_recovery_uses_existing_uuid_not_prepared_uuid(tmp_path: Path) -> None:
    """Fresh-process recovery: BUILDING UUID A on disk, prepared UUID B discarded."""
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    _promote_to_final(snapshots_directory, prepared_a)
    _create_building_row(database, prepared_a)
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)
    assert prepared_b.temporary_directory.exists()
    assert prepared_b.snapshot_id != UUID_A

    # A new service instance simulates a restarted process; prepared_a is never reused.
    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_A
    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_relative_path == prepared_a.relative_manifest_path
    assert not prepared_b.temporary_directory.exists()
    assert not resolve_snapshot_directory(
        snapshots_directory,
        prepared_b.relative_directory,
    ).exists()
    records = _records(database)
    assert len(records) == 1
    assert records[0].id == UUID_A
    assert records[0].status is SnapshotStatus.READY


def test_building_missing_directory_keeps_row_and_discards_prepared(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    building = _create_building_row(database, prepared_a)
    shutil.rmtree(prepared_a.temporary_directory, ignore_errors=True)
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    with pytest.raises(SnapshotBusyError, match="BUILDING metadata"):
        publication_service(database, snapshots_directory).publish_or_reuse(
            prepared_b,
            actor="test",
        )

    assert not prepared_b.temporary_directory.exists()
    records = _records(database)
    assert len(records) == 1
    assert records[0].id == UUID_A
    assert records[0].status is SnapshotStatus.BUILDING
    assert records[0].version == building.version
    assert records[0].checksum_sha256 is None


def test_orphan_discovery_adopts_a_different_uuid(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    final_directory = _orphan(tmp_path, snapshots_directory)
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_A
    assert published.snapshot_reused is False
    assert published.snapshot_relative_path.endswith(f"{UUID_A}/{MANIFEST_FILENAME}")
    assert published.row_count("events") == 1
    # Every published value comes from the adopted manifest, never from the prepared one.
    assert published.manifest_checksum_sha256 != prepared_b.manifest_checksum_sha256
    assert final_directory.exists()
    assert not prepared_b.temporary_directory.exists()
    assert not resolve_snapshot_directory(
        snapshots_directory,
        prepared_b.relative_directory,
    ).exists()
    records = _records(database)
    assert len(records) == 1
    assert records[0].id == UUID_A
    assert records[0].checksum_sha256 == published.manifest_checksum_sha256
    assert records[0].row_count == published.row_count("events")


@pytest.mark.parametrize(
    "build_orphan",
    [
        pytest.param(_different_competition_orphan, id="different_competition"),
        pytest.param(_different_season_orphan, id="different_season"),
        pytest.param(_different_source_version_orphan, id="different_source_version"),
        pytest.param(_different_raw_checksum_orphan, id="different_raw_checksum"),
        pytest.param(_different_schema_version_orphan, id="different_schema_version"),
    ],
)
def test_orphan_with_a_mismatched_identity_is_not_adopted(
    tmp_path: Path,
    build_orphan: OrphanFactory,
) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    orphan_directory = build_orphan(tmp_path, snapshots_directory)
    orphan_manifest = (orphan_directory / MANIFEST_FILENAME).read_bytes()
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert published.manifest_checksum_sha256 == prepared.manifest_checksum_sha256
    assert resolve_snapshot_directory(snapshots_directory, prepared.relative_directory).exists()
    assert (orphan_directory / MANIFEST_FILENAME).read_bytes() == orphan_manifest
    records = _records(database)
    assert len(records) == 1
    assert records[0].id == UUID_B


def test_two_identity_matching_orphans_are_rejected(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    for snapshot_id in (UUID_A, UUID_C):
        _orphan(tmp_path, snapshots_directory, snapshot_id=snapshot_id)
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    with pytest.raises(SnapshotIntegrityError, match="multiple identity-matching orphan"):
        publication_service(database, snapshots_directory).publish_or_reuse(
            prepared_b,
            actor="test",
        )

    assert not prepared_b.temporary_directory.exists()
    assert _records(database) == []


def test_malformed_orphan_candidate_is_ignored(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    parent = resolve_snapshot_directory(snapshots_directory, prepared_a.relative_directory).parent
    parent.mkdir(parents=True, exist_ok=True)
    malformed = parent / UUID_C
    malformed.mkdir()
    (malformed / MANIFEST_FILENAME).write_text("{not-json", encoding="utf-8")
    shutil.rmtree(prepared_a.temporary_directory, ignore_errors=True)
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert (malformed / MANIFEST_FILENAME).read_text(encoding="utf-8") == "{not-json"


def test_non_uuid_child_directory_is_ignored(tmp_path: Path) -> None:
    """Children whose names are not canonical UUIDs are not project-owned candidates."""
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    orphan_directory = _orphan(tmp_path, snapshots_directory)
    candidate = orphan_directory.parent / "not-a-uuid"
    orphan_directory.rename(candidate)
    candidate_files = sorted(item.name for item in candidate.iterdir())
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert sorted(item.name for item in candidate.iterdir()) == candidate_files


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform lacks symlink support")
def test_symlink_orphan_candidate_is_rejected(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    final_directory = _orphan(tmp_path, snapshots_directory)
    link = final_directory.parent / UUID_C
    try:
        link.symlink_to(final_directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink not permitted: {exc}")
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    # The symlink is skipped, so exactly one candidate matches and UUID_A is adopted.
    assert published.snapshot_id == UUID_A
    assert link.is_symlink()


def test_verify_snapshot_directory_never_called_in_write_transaction(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    in_write_tx = {"active": False}
    observed_during_write_tx: list[bool] = []
    original_verify = verify_snapshot_directory
    original_transaction = transaction

    @contextmanager
    def tracking_transaction(
        connection: sqlite3.Connection,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with original_transaction(connection, immediate=immediate) as active:
            in_write_tx["active"] = True
            try:
                yield active
            finally:
                in_write_tx["active"] = False

    def instrumented(
        *,
        snapshots_directory: Path,
        relative_manifest_path: str,
        suite: SnapshotDatasetSuite,
        expected_snapshot: SnapshotRecord | None = None,
    ) -> SnapshotVerificationResult:
        observed_during_write_tx.append(in_write_tx["active"])
        return original_verify(
            snapshots_directory=snapshots_directory,
            relative_manifest_path=relative_manifest_path,
            suite=suite,
            expected_snapshot=expected_snapshot,
        )

    with patch(
        "sports_analytics.snapshots.service.transaction",
        side_effect=tracking_transaction,
    ):
        with patch(
            "sports_analytics.snapshots.service.verify_snapshot_directory",
            side_effect=instrumented,
        ):
            first = publication_service(database, snapshots_directory).publish_or_reuse(
                prepared,
                actor="test",
            )
            second = prepare(
                tmp_path,
                snapshots_directory=snapshots_directory,
                snapshot_id=UUID_B,
            )
            reused = publication_service(database, snapshots_directory).publish_or_reuse(
                second,
                actor="test",
            )

    assert first.snapshot_reused is False
    assert reused.snapshot_reused is True
    assert observed_during_write_tx
    assert all(flag is False for flag in observed_during_write_tx)


def test_ready_reuse_discards_prepared_temp(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    first = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    publication_service(database, snapshots_directory).publish_or_reuse(first, actor="test")
    second = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)
    assert second.temporary_directory.exists()

    reused = publication_service(database, snapshots_directory).publish_or_reuse(
        second,
        actor="test",
    )

    assert reused.snapshot_reused is True
    assert not second.temporary_directory.exists()
    assert not resolve_snapshot_directory(snapshots_directory, second.relative_directory).exists()

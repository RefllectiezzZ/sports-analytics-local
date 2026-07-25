"""Snapshot publication service tests driven through the football dataset suite.

The publication service is sport-agnostic: every domain fact reaches it through a
validated snapshot identity and dataset suite, so these tests assert generic row
counts and partition values instead of sport-specific result fields.
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import (
    SnapshotBusyError,
    SnapshotIntegrityError,
    SnapshotVerificationError,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JsonValue, SnapshotRecord, SnapshotStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.snapshots.service import SnapshotPublicationService
from sports_analytics.snapshots.spec import MANIFEST_FILENAME, SnapshotDatasetSuite
from sports_analytics.snapshots.types import PublishedSnapshot
from sports_analytics.snapshots.writer import PreparedSnapshot, resolve_snapshot_directory
from sports_analytics.sports.football.schemas import football_snapshot_suite
from tests.helpers_snapshots import (
    SYNTHETIC_CSV,
    SYNTHETIC_CSV_WITH_ODDS,
    database_path,
    prepare,
    publication_service,
)

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"
UUID_FOREIGN = "99999999-9999-4999-8999-999999999999"

EXPECTED_PARENT = "football-ingestion/football-canonical-v2/eng-premier-league/2023-2024"


def _records(database: Path) -> list[SnapshotRecord]:
    """Return every snapshot metadata row, closing the connection before assertions."""
    with connect_database(database, read_only=True) as connection:
        return SnapshotRepository(connection).list_snapshots()


def _create_building_row(
    database: Path,
    prepared: PreparedSnapshot,
    *,
    snapshot_id: str,
) -> SnapshotRecord:
    """Insert a BUILDING row for a prepared snapshot under an explicit UUID."""
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
                snapshot_id=snapshot_id,
                source_version=prepared.source_version,
                created_at=prepared.source_observed_at_utc,
            )


def _promote_to_final(snapshots_directory: Path, prepared: PreparedSnapshot) -> Path:
    """Move a prepared temporary directory to its final immutable location."""
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared.temporary_directory.rename(final_directory)
    return final_directory


def _suite_without(dataset_name: str) -> SnapshotDatasetSuite:
    """Build a deliberately different suite by dropping one football descriptor."""
    suite = football_snapshot_suite()
    return SnapshotDatasetSuite(
        descriptors=tuple(
            descriptor
            for descriptor in suite.descriptors
            if descriptor.dataset_name != dataset_name
        ),
        primary_dataset_name=suite.primary_dataset_name,
    )


def _suite_with_mutated_events_descriptor(*, mutation: str) -> SnapshotDatasetSuite:
    suite = football_snapshot_suite()
    descriptors = list(suite.descriptors)
    index = suite.dataset_names.index("events")
    descriptor = descriptors[index]
    if mutation == "filename":
        descriptors[index] = replace(descriptor, relative_filename="canonical_events.parquet")
    elif mutation == "schema":
        field_index = descriptor.schema.get_field_index("competition_id")
        schema = descriptor.schema.set(
            field_index,
            descriptor.schema.field(field_index).with_nullable(True),
        )
        descriptors[index] = replace(descriptor, schema=schema)
    else:  # pragma: no cover - defensive for future test edits
        raise AssertionError(f"unknown mutation {mutation}")
    return SnapshotDatasetSuite(
        descriptors=tuple(descriptors),
        primary_dataset_name=suite.primary_dataset_name,
    )


def test_publish_prepared_snapshot_marks_ready_and_verifies_directory(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
        content=SYNTHETIC_CSV_WITH_ODDS,
    )

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
        correlation_id="job-1",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.snapshot_type == "football-ingestion"
    assert published.schema_version == "football-canonical-v2"
    assert published.snapshot_relative_path == f"{EXPECTED_PARENT}/{UUID_A}/{MANIFEST_FILENAME}"
    assert published.row_count("events") == 1
    assert published.row_count("participants") == 2
    assert published.row_count("market_quotes") == 3
    assert published.row_count("post_match_statistics") == 1
    assert published.partition_value("competition_id") == "eng-premier-league"
    assert published.partition_value("season_label") == "2023-2024"

    result = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=published.snapshot_relative_path,
        suite=football_snapshot_suite(),
    )

    assert result.snapshot_id == published.snapshot_id
    assert result.manifest_checksum_sha256 == published.manifest_checksum_sha256
    assert result.raw_artifact_sha256 == published.raw_artifact_sha256
    assert result.row_counts == published.metrics.row_counts
    assert result.primary_row_count == 1
    assert result.partition_keys == published.partition_keys
    assert result.file_count == 10


def test_ready_snapshot_is_reused_for_same_source_version(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    service = publication_service(database, snapshots_directory)
    first = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    first_published = service.publish_or_reuse(first, actor="test")
    second = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    reused = service.publish_or_reuse(second, actor="test")

    assert reused.snapshot_reused is True
    assert reused.snapshot_id == first_published.snapshot_id
    assert reused.snapshot_relative_path == first_published.snapshot_relative_path
    assert reused.row_count("events") == first_published.row_count("events")
    assert not second.temporary_directory.exists()
    assert not resolve_snapshot_directory(snapshots_directory, second.relative_directory).exists()
    assert [item.status for item in _records(database)] == [SnapshotStatus.READY]


def test_building_metadata_without_final_directory_is_busy(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    building = _create_building_row(database, prepared, snapshot_id=UUID_FOREIGN)

    with pytest.raises(SnapshotBusyError, match="BUILDING metadata"):
        publication_service(database, snapshots_directory).publish_or_reuse(prepared, actor="test")

    assert not prepared.temporary_directory.exists()
    records = _records(database)
    assert [item.status for item in records] == [SnapshotStatus.BUILDING]
    assert records[0].id == building.id
    assert records[0].version == building.version
    assert records[0].checksum_sha256 is None


def test_building_metadata_with_existing_final_directory_is_completed(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    checksum = prepared.manifest_checksum_sha256
    _promote_to_final(snapshots_directory, prepared)
    _create_building_row(database, prepared, snapshot_id=prepared.snapshot_id)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.snapshot_id == UUID_A
    assert published.manifest_checksum_sha256 == checksum
    assert published.row_count("events") == 1
    assert [item.status for item in _records(database)] == [SnapshotStatus.READY]


def test_orphan_final_directory_is_adopted_when_metadata_is_missing(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    final_directory = _promote_to_final(snapshots_directory, prepared)
    manifest_bytes = (final_directory / MANIFEST_FILENAME).read_bytes()

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.snapshot_id == UUID_A
    assert (final_directory / MANIFEST_FILENAME).read_bytes() == manifest_bytes
    assert (
        verify_snapshot_directory(
            snapshots_directory=snapshots_directory,
            relative_manifest_path=published.snapshot_relative_path,
            suite=football_snapshot_suite(),
        ).row_count("events")
        == 1
    )


def test_conflicting_directory_at_prepared_path_is_not_overwritten(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared.relative_directory)
    final_directory.mkdir(parents=True)
    (final_directory / MANIFEST_FILENAME).write_text("not valid\n", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="conflicting snapshot directory"):
        publication_service(database, snapshots_directory).publish_or_reuse(prepared, actor="test")

    assert not prepared.temporary_directory.exists()
    assert (final_directory / MANIFEST_FILENAME).read_text(encoding="utf-8") == "not valid\n"
    assert sorted(item.name for item in final_directory.iterdir()) == [MANIFEST_FILENAME]
    assert _records(database) == []


def test_failed_snapshot_row_does_not_block_replacement(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    failed = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    building = _create_building_row(database, failed, snapshot_id=failed.snapshot_id)
    with connect_database(database) as connection:
        with transaction(connection, immediate=True):
            SnapshotRepository(connection).mark_snapshot_failed(
                building.id,
                expected_version=building.version,
                metadata={"reason": "test failure"},
            )
    # The failed publication never reached the filesystem; drop its temporary bytes.
    shutil.rmtree(failed.temporary_directory, ignore_errors=True)
    replacement = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        replacement,
        actor="test",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.snapshot_id == replacement.snapshot_id
    assert sorted(item.status for item in _records(database)) == [
        SnapshotStatus.FAILED,
        SnapshotStatus.READY,
    ]


def test_concurrent_publish_of_same_source_version_creates_one_ready(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    prepared_b = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_B)
    barrier = threading.Barrier(3)

    def publish(prepared: PreparedSnapshot) -> PublishedSnapshot:
        barrier.wait(timeout=5)
        return publication_service(database, snapshots_directory).publish_or_reuse(
            prepared,
            actor="test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(publish, prepared_a)
        future_b = executor.submit(publish, prepared_b)
        barrier.wait(timeout=5)
        results = [future_a.result(timeout=30), future_b.result(timeout=30)]

    assert sorted(item.snapshot_reused for item in results) == [False, True]
    assert {item.snapshot_id for item in results} == {results[0].snapshot_id}
    records = _records(database)
    assert len(records) == 1
    assert records[0].status is SnapshotStatus.READY


def test_distinct_source_versions_stay_active_while_a_repeat_is_deduplicated(
    tmp_path: Path,
) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    service = publication_service(database, snapshots_directory)
    plain = prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
        content=SYNTHETIC_CSV,
    )
    with_odds = prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
        content=SYNTHETIC_CSV_WITH_ODDS,
    )
    assert plain.source_version != with_odds.source_version

    first = service.publish_or_reuse(plain, actor="test")
    second = service.publish_or_reuse(with_odds, actor="test")

    assert first.snapshot_reused is False
    assert second.snapshot_reused is False
    assert first.source_version != second.source_version
    assert [item.status for item in _records(database)] == [
        SnapshotStatus.READY,
        SnapshotStatus.READY,
    ]

    repeat = prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_C,
        content=SYNTHETIC_CSV,
    )

    reused = service.publish_or_reuse(repeat, actor="test")

    assert reused.snapshot_reused is True
    assert reused.snapshot_id == first.snapshot_id
    assert len(_records(database)) == 2


def test_verification_rejects_a_mismatched_dataset_suite(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)

    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )
    verified = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=published.snapshot_relative_path,
        suite=football_snapshot_suite(),
    )

    assert verified.row_counts == published.metrics.row_counts

    with pytest.raises(SnapshotVerificationError, match="schema_fingerprints keys mismatch"):
        verify_snapshot_directory(
            snapshots_directory=snapshots_directory,
            relative_manifest_path=published.snapshot_relative_path,
            suite=_suite_without("post_match_statistics"),
        )


def test_publication_service_rejects_a_foreign_dataset_suite(tmp_path: Path) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    service = SnapshotPublicationService(
        database_path=database,
        snapshots_directory=snapshots_directory,
        suite=_suite_without("post_match_statistics"),
    )

    with pytest.raises(SnapshotIntegrityError, match="does not match the publication service"):
        service.publish_or_reuse(prepared, actor="test")

    # The suite is rejected before ownership transfers, so the caller still owns the bytes.
    assert prepared.temporary_directory.exists()
    assert _records(database) == []

    service.discard_prepared(prepared)

    assert not prepared.temporary_directory.exists()


@pytest.mark.parametrize("mutation", ["filename", "schema"])
def test_publication_service_rejects_same_dataset_names_with_different_suite_contract(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = database_path(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = prepare(tmp_path, snapshots_directory=snapshots_directory, snapshot_id=UUID_A)
    service = SnapshotPublicationService(
        database_path=database,
        snapshots_directory=snapshots_directory,
        suite=_suite_with_mutated_events_descriptor(mutation=mutation),
    )

    with pytest.raises(SnapshotIntegrityError, match="does not match the publication service"):
        service.publish_or_reuse(prepared, actor="test")

    assert prepared.temporary_directory.exists()
    assert _records(database) == []

    service.discard_prepared(prepared)

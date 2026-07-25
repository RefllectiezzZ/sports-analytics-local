"""Section 57: football snapshot publication service tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import SnapshotBusyError, SnapshotIntegrityError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.snapshots.service import SnapshotPublicationService
from sports_analytics.snapshots.writer import (
    PreparedSnapshot,
    prepare_snapshot_directory,
    resolve_snapshot_directory,
)
from sports_analytics.sources.football_data_co_uk.catalog import build_csv_url
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv
from sports_analytics.sources.raw_store import RawSourceArtifact, RawSourceStore
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.contracts import FOOTBALL_INGESTION_SNAPSHOT_TYPE
from sports_analytics.sports.football.normalization import normalize_football_rows

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
SYNTHETIC_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)


def _database(tmp_path: Path) -> Path:
    database_path = tmp_path / "operational.sqlite3"
    ensure_database_ready(database_path)
    return database_path


def _artifact(raw_directory: Path) -> RawSourceArtifact:
    return RawSourceStore(raw_directory).store_bytes(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        source_url=build_csv_url(division_code="E0", source_season_code="2324"),
        content=SYNTHETIC_CSV,
        retrieved_at=OBSERVED_AT,
        content_type="text/csv",
        etag='"etag-1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        encoding="utf-8",
    )


def _bundle(artifact: RawSourceArtifact):
    parsed = parse_football_data_csv(SYNTHETIC_CSV, expected_division_code="E0")
    return normalize_football_rows(
        rows=list(parsed.rows),
        competition_id="eng-premier-league",
        competition_display_name="Premier League",
        country_code="ENG",
        source_competition_code="E0",
        timezone_name="Europe/London",
        season_label="2023-2024",
        start_year=2023,
        end_year=2024,
        source_season_code="2324",
        source_name=artifact.source_name,
        source_file_sha256=artifact.checksum_sha256,
        source_observed_at_utc=OBSERVED_AT,
    )


def _prepare(
    tmp_path: Path,
    *,
    snapshot_id: str,
    snapshots_directory: Path | None = None,
) -> PreparedSnapshot:
    root = snapshots_directory if snapshots_directory is not None else tmp_path / "snapshots"
    artifact = _artifact(tmp_path / "raw")
    return prepare_snapshot_directory(
        snapshots_directory=root,
        bundle=_bundle(artifact),
        artifact=artifact,
        competition_id="eng-premier-league",
        season_label="2023-2024",
        source_competition_code="E0",
        source_season_code="2324",
        source_url=artifact.source_url,
        source_observed_at_utc=OBSERVED_AT,
        unknown_source_columns=(),
        missing_optional_source_columns=("Time",),
        http_status=200,
        http_content_type=artifact.content_type,
        http_content_length=artifact.byte_count,
        http_etag=artifact.etag,
        http_last_modified=artifact.last_modified,
        http_final_url=artifact.source_url,
        snapshot_id=snapshot_id,
    )


def _service(database_path: Path, snapshots_directory: Path) -> SnapshotPublicationService:
    return SnapshotPublicationService(
        database_path=database_path,
        snapshots_directory=snapshots_directory,
    )


def _snapshots(database_path: Path):
    with connect_database(database_path, read_only=True) as connection:
        return SnapshotRepository(connection).list_snapshots()


def test_publish_prepared_snapshot_marks_ready_and_verifies_directory(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
        correlation_id="job-1",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.games_count == 1
    assert published.teams_count == 2
    assert published.odds_quotes_count == 0
    assert published.statistics_rows_count == 0
    result = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=published.snapshot_relative_path,
    )
    assert result.snapshot_id == published.snapshot_id
    assert result.manifest_checksum_sha256 == published.manifest_checksum_sha256


def test_ready_snapshot_is_reused_for_same_source_version(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    service = _service(database_path, snapshots_directory)
    first = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    first_published = service.publish_or_reuse(first, actor="test")
    second = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="22222222-2222-4222-8222-222222222222",
    )

    reused = service.publish_or_reuse(second, actor="test")

    assert reused.snapshot_reused is True
    assert reused.snapshot_id == first_published.snapshot_id
    assert reused.snapshot_relative_path == first_published.snapshot_relative_path
    assert not second.temporary_directory.exists()
    assert [item.status for item in _snapshots(database_path)] == [SnapshotStatus.READY]


def test_building_metadata_without_final_directory_is_busy(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            SnapshotRepository(connection).create_building_snapshot(
                snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                relative_path=prepared.relative_manifest_path,
                source_name=prepared.source_name,
                schema_version=prepared.schema_version,
                metadata=prepared.metadata,
                snapshot_id="99999999-9999-4999-8999-999999999999",
                source_version=prepared.source_version,
                created_at=prepared.source_observed_at_utc,
            )

    with pytest.raises(SnapshotBusyError, match="BUILDING metadata"):
        _service(database_path, snapshots_directory).publish_or_reuse(prepared, actor="test")

    assert not prepared.temporary_directory.exists()
    assert [item.status for item in _snapshots(database_path)] == [SnapshotStatus.BUILDING]


def test_building_metadata_with_existing_final_directory_is_completed(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared.temporary_directory.rename(final_directory)
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            SnapshotRepository(connection).create_building_snapshot(
                snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                relative_path=prepared.relative_manifest_path,
                source_name=prepared.source_name,
                schema_version=prepared.schema_version,
                metadata=prepared.metadata,
                snapshot_id=prepared.snapshot_id,
                source_version=prepared.source_version,
                created_at=prepared.source_observed_at_utc,
            )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.manifest_checksum_sha256 == prepared.manifest_checksum_sha256
    assert [item.status for item in _snapshots(database_path)] == [SnapshotStatus.READY]


def test_orphan_final_directory_is_adopted_when_metadata_is_missing(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared.temporary_directory.rename(final_directory)

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_reused is False
    assert published.snapshot_id == prepared.snapshot_id
    assert verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=published.snapshot_relative_path,
    ).games_count == 1


def test_conflicting_orphan_directory_is_not_overwritten(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared.relative_directory)
    final_directory.mkdir(parents=True)
    (final_directory / "manifest.json").write_text("not valid\n", encoding="utf-8")

    with pytest.raises(SnapshotIntegrityError, match="conflicting snapshot directory"):
        _service(database_path, snapshots_directory).publish_or_reuse(prepared, actor="test")

    assert not prepared.temporary_directory.exists()
    assert (final_directory / "manifest.json").read_text(encoding="utf-8") == "not valid\n"


def test_failed_snapshot_row_does_not_block_replacement(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    failed_prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            repository = SnapshotRepository(connection)
            building = repository.create_building_snapshot(
                snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                relative_path=failed_prepared.relative_manifest_path,
                source_name=failed_prepared.source_name,
                schema_version=failed_prepared.schema_version,
                metadata=failed_prepared.metadata,
                snapshot_id=failed_prepared.snapshot_id,
                source_version=failed_prepared.source_version,
                created_at=failed_prepared.source_observed_at_utc,
            )
            repository.mark_snapshot_failed(
                building.id,
                expected_version=building.version,
                metadata={"reason": "test failure"},
            )
    replacement = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="22222222-2222-4222-8222-222222222222",
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        replacement,
        actor="test",
    )

    statuses = sorted(item.status for item in _snapshots(database_path))
    assert statuses == [SnapshotStatus.FAILED, SnapshotStatus.READY]
    assert published.snapshot_status is SnapshotStatus.READY
    assert published.snapshot_id == replacement.snapshot_id

    # The failed prepared temp directory was never published; keep the test filesystem tidy.
    if failed_prepared.temporary_directory.exists():
        import shutil

        shutil.rmtree(failed_prepared.temporary_directory)


def test_concurrent_publish_of_same_source_version_creates_one_ready(
    tmp_path: Path,
) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="11111111-1111-4111-8111-111111111111",
    )
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id="22222222-2222-4222-8222-222222222222",
    )
    barrier = threading.Barrier(3)

    def publish(prepared: PreparedSnapshot):
        barrier.wait(timeout=5)
        return _service(database_path, snapshots_directory).publish_or_reuse(
            prepared,
            actor="test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(publish, prepared_a)
        future_b = executor.submit(publish, prepared_b)
        barrier.wait(timeout=5)
        results = [future_a.result(timeout=10), future_b.result(timeout=10)]

    assert sorted(item.snapshot_reused for item in results) == [False, True]
    assert {item.snapshot_id for item in results} == {results[0].snapshot_id}
    records = _snapshots(database_path)
    assert len(records) == 1
    assert records[0].status is SnapshotStatus.READY

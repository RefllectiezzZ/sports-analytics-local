"""Regression tests for BUILDING recovery, orphan adoption, and TX boundaries."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from sports_analytics.core.exceptions import SnapshotBusyError, SnapshotIntegrityError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import SnapshotStatus
from sports_analytics.snapshots import reader as snapshot_reader
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
UUID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UUID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UUID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _database(tmp_path: Path) -> Path:
    database_path = tmp_path / "operational.sqlite3"
    ensure_database_ready(database_path)
    return database_path


def _artifact(raw_directory: Path, *, content: bytes = SYNTHETIC_CSV) -> RawSourceArtifact:
    return RawSourceStore(raw_directory).store_bytes(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        source_url=build_csv_url(division_code="E0", source_season_code="2324"),
        content=content,
        retrieved_at=OBSERVED_AT,
        content_type="text/csv",
        etag='"etag-1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        encoding="utf-8",
    )


def _bundle(
    artifact: RawSourceArtifact,
    *,
    competition_id: str = "eng-premier-league",
    season_label: str = "2023-2024",
    source_competition_code: str = "E0",
    source_season_code: str = "2324",
    start_year: int = 2023,
    end_year: int = 2024,
):
    parsed = parse_football_data_csv(SYNTHETIC_CSV, expected_division_code=source_competition_code)
    return normalize_football_rows(
        rows=list(parsed.rows),
        competition_id=competition_id,
        competition_display_name="Premier League",
        country_code="ENG",
        source_competition_code=source_competition_code,
        timezone_name="Europe/London",
        season_label=season_label,
        start_year=start_year,
        end_year=end_year,
        source_season_code=source_season_code,
        source_name=artifact.source_name,
        source_file_sha256=artifact.checksum_sha256,
        source_observed_at_utc=OBSERVED_AT,
    )


def _prepare(
    tmp_path: Path,
    *,
    snapshot_id: str,
    snapshots_directory: Path | None = None,
    competition_id: str = "eng-premier-league",
    season_label: str = "2023-2024",
    source_competition_code: str = "E0",
    source_season_code: str = "2324",
    content: bytes = SYNTHETIC_CSV,
) -> PreparedSnapshot:
    root = snapshots_directory if snapshots_directory is not None else tmp_path / "snapshots"
    artifact = _artifact(tmp_path / f"raw-{snapshot_id}", content=content)
    start_year = int(season_label.split("-")[0])
    end_year = int(season_label.split("-")[1])
    return prepare_snapshot_directory(
        snapshots_directory=root,
        bundle=_bundle(
            artifact,
            competition_id=competition_id,
            season_label=season_label,
            source_competition_code=source_competition_code,
            source_season_code=source_season_code,
            start_year=start_year,
            end_year=end_year,
        ),
        artifact=artifact,
        competition_id=competition_id,
        season_label=season_label,
        source_competition_code=source_competition_code,
        source_season_code=source_season_code,
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


def _publish_building_final(
    *,
    database_path: Path,
    snapshots_directory: Path,
    prepared: PreparedSnapshot,
) -> None:
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


def test_building_recovery_uses_existing_uuid_not_prepared_uuid(tmp_path: Path) -> None:
    """Fresh-process recovery: BUILDING UUID A on disk, prepared UUID B discarded."""
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    _publish_building_final(
        database_path=database_path,
        snapshots_directory=snapshots_directory,
        prepared=prepared_a,
    )
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )
    assert prepared_b.temporary_directory.exists()
    assert prepared_b.snapshot_id != UUID_A

    # New service instance simulates a restarted process; do not reuse prepared_a.
    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_A
    assert published.snapshot_status is SnapshotStatus.READY
    assert not prepared_b.temporary_directory.exists()
    assert not resolve_snapshot_directory(
        snapshots_directory, prepared_b.relative_directory
    ).exists()
    with connect_database(database_path, read_only=True) as connection:
        records = SnapshotRepository(connection).list_snapshots()
    assert len(records) == 1
    assert records[0].id == UUID_A
    assert records[0].status is SnapshotStatus.READY


def test_building_missing_directory_keeps_row_and_discards_prepared(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            SnapshotRepository(connection).create_building_snapshot(
                snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                relative_path=prepared_a.relative_manifest_path,
                source_name=prepared_a.source_name,
                schema_version=prepared_a.schema_version,
                metadata=prepared_a.metadata,
                snapshot_id=prepared_a.snapshot_id,
                source_version=prepared_a.source_version,
                created_at=prepared_a.source_observed_at_utc,
            )
    shutil.rmtree(prepared_a.temporary_directory, ignore_errors=True)
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    with pytest.raises(SnapshotBusyError, match="BUILDING metadata"):
        _service(database_path, snapshots_directory).publish_or_reuse(prepared_b, actor="test")

    assert not prepared_b.temporary_directory.exists()
    with connect_database(database_path, read_only=True) as connection:
        records = SnapshotRepository(connection).list_snapshots()
    assert len(records) == 1
    assert records[0].status is SnapshotStatus.BUILDING
    assert records[0].id == UUID_A


def test_orphan_discovery_adopts_different_uuid(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared_a.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared_a.temporary_directory.rename(final_directory)
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_A
    assert published.games_count == 1
    assert published.manifest_checksum_sha256 != prepared_b.manifest_checksum_sha256
    assert not prepared_b.temporary_directory.exists()
    assert not resolve_snapshot_directory(
        snapshots_directory, prepared_b.relative_directory
    ).exists()
    with connect_database(database_path, read_only=True) as connection:
        records = SnapshotRepository(connection).list_snapshots()
    assert len(records) == 1
    assert records[0].id == UUID_A
    assert records[0].checksum_sha256 == published.manifest_checksum_sha256


def test_orphan_different_source_version_is_not_adopted(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    other_csv = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        b"E0,13/08/2023,Northbridge FC,Southport Athletic,1,0,H\n"
    )
    prepared_other = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
        content=other_csv,
    )
    final_directory = resolve_snapshot_directory(
        snapshots_directory, prepared_other.relative_directory
    )
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared_other.temporary_directory.rename(final_directory)
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert resolve_snapshot_directory(snapshots_directory, prepared.relative_directory).exists()
    assert final_directory.exists()


def test_orphan_different_season_not_under_parent(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    foreign = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
        season_label="2022-2023",
        source_season_code="2223",
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, foreign.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    foreign.temporary_directory.rename(final_directory)
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert final_directory.exists()


def test_orphan_identity_mismatch_under_same_parent_is_not_adopted(tmp_path: Path) -> None:
    """A valid sibling with different raw hash must not be adopted."""
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    other_csv = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        b"E0,14/08/2023,Northbridge FC,Southport Athletic,3,0,H\n"
    )
    foreign = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
        content=other_csv,
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, foreign.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    foreign.temporary_directory.rename(final_directory)
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert published.source_file_sha256 != foreign.source_file_sha256
    assert final_directory.exists()


def test_two_matching_orphans_rejected(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    for snapshot_id in (UUID_A, UUID_C):
        prepared = _prepare(
            tmp_path,
            snapshots_directory=snapshots_directory,
            snapshot_id=snapshot_id,
        )
        final_directory = resolve_snapshot_directory(
            snapshots_directory, prepared.relative_directory
        )
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        prepared.temporary_directory.rename(final_directory)
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    with pytest.raises(SnapshotIntegrityError, match="multiple identity-matching orphan"):
        _service(database_path, snapshots_directory).publish_or_reuse(prepared_b, actor="test")

    assert not prepared_b.temporary_directory.exists()


def test_malformed_orphan_candidate_is_ignored(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    parent = resolve_snapshot_directory(snapshots_directory, prepared_a.relative_directory).parent
    parent.mkdir(parents=True, exist_ok=True)
    bad = parent / UUID_C
    bad.mkdir()
    (bad / "manifest.json").write_text("{not-json", encoding="utf-8")
    shutil.rmtree(prepared_a.temporary_directory, ignore_errors=True)
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    assert published.snapshot_id == UUID_B
    assert bad.exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform lacks symlink support")
def test_symlink_orphan_candidate_rejected(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared_a = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    final_directory = resolve_snapshot_directory(snapshots_directory, prepared_a.relative_directory)
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    prepared_a.temporary_directory.rename(final_directory)
    link = final_directory.parent / UUID_C
    try:
        link.symlink_to(final_directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink not permitted: {exc}")
    prepared_b = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )

    published = _service(database_path, snapshots_directory).publish_or_reuse(
        prepared_b,
        actor="test",
    )

    # Symlink ignored; the real UUID_A orphan is adopted.
    assert published.snapshot_id == UUID_A


def test_verify_snapshot_directory_never_called_in_write_transaction(
    tmp_path: Path,
) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    prepared = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    in_write_tx = {"active": False}
    observed_during_write_tx: list[bool] = []
    original_verify = snapshot_reader.verify_snapshot_directory
    original_transaction = __import__(
        "sports_analytics.data.database", fromlist=["transaction"]
    ).transaction

    from contextlib import contextmanager

    @contextmanager
    def tracking_transaction(connection, *, immediate: bool = False):
        with original_transaction(connection, immediate=immediate) as conn:
            in_write_tx["active"] = True
            try:
                yield conn
            finally:
                in_write_tx["active"] = False

    def instrumented(*args, **kwargs):
        observed_during_write_tx.append(in_write_tx["active"])
        return original_verify(*args, **kwargs)

    with patch(
        "sports_analytics.snapshots.service.transaction",
        side_effect=tracking_transaction,
    ):
        with patch(
            "sports_analytics.snapshots.service.verify_snapshot_directory",
            side_effect=instrumented,
        ):
            first = _service(database_path, snapshots_directory).publish_or_reuse(
                prepared,
                actor="test",
            )
            second = _prepare(
                tmp_path,
                snapshots_directory=snapshots_directory,
                snapshot_id=UUID_B,
            )
            reused = _service(database_path, snapshots_directory).publish_or_reuse(
                second,
                actor="test",
            )

    assert first.snapshot_reused is False
    assert reused.snapshot_reused is True
    assert observed_during_write_tx
    assert all(flag is False for flag in observed_during_write_tx)


def test_ready_reuse_discards_prepared_temp(tmp_path: Path) -> None:
    database_path = _database(tmp_path)
    snapshots_directory = tmp_path / "snapshots"
    first = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_A,
    )
    _service(database_path, snapshots_directory).publish_or_reuse(first, actor="test")
    second = _prepare(
        tmp_path,
        snapshots_directory=snapshots_directory,
        snapshot_id=UUID_B,
    )
    assert second.temporary_directory.exists()

    reused = _service(database_path, snapshots_directory).publish_or_reuse(second, actor="test")

    assert reused.snapshot_reused is True
    assert not second.temporary_directory.exists()

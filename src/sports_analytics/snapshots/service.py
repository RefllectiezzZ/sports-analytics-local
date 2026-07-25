"""Publish prepared football snapshots with short SQLite transactions."""

from __future__ import annotations

import shutil
from pathlib import Path

from sports_analytics.core.exceptions import (
    DatabaseIntegrityError,
    SnapshotBusyError,
    SnapshotIntegrityError,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.audit import AuditEventRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import JsonValue, SnapshotStatus
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.snapshots.types import PublishedSnapshot
from sports_analytics.snapshots.writer import (
    PreparedSnapshot,
    discard_prepared_snapshot,
    resolve_snapshot_directory,
)
from sports_analytics.sports.football.contracts import FOOTBALL_INGESTION_SNAPSHOT_TYPE


class SnapshotPublicationService:
    """Coordinate filesystem publication with SnapshotRepository metadata."""

    def __init__(self, *, database_path: Path, snapshots_directory: Path) -> None:
        self._database_path = Path(database_path)
        self._snapshots_directory = Path(snapshots_directory)

    def publish_or_reuse(
        self,
        prepared: PreparedSnapshot,
        *,
        actor: str,
        correlation_id: str | None = None,
    ) -> PublishedSnapshot:
        """Publish a prepared snapshot or reuse an existing READY snapshot.

        Long-running work must already be complete in ``prepared``. This method
        opens only a short BEGIN IMMEDIATE transaction for metadata and the
        same-filesystem atomic rename.
        """
        final_directory = resolve_snapshot_directory(
            self._snapshots_directory,
            prepared.relative_directory,
        )
        try:
            with connect_database(self._database_path) as connection:
                with transaction(connection, immediate=True):
                    snapshots = SnapshotRepository(connection)
                    audit = AuditEventRepository(connection)
                    existing = snapshots.get_active_snapshot_by_source_version(
                        snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                        source_name=prepared.source_name,
                        source_version=prepared.source_version,
                        schema_version=prepared.schema_version,
                    )
                    if existing is not None and existing.status is SnapshotStatus.READY:
                        verify_snapshot_directory(
                            snapshots_directory=self._snapshots_directory,
                            relative_manifest_path=existing.relative_path,
                            expected_snapshot=existing,
                        )
                        discard_prepared_snapshot(prepared)
                        audit.append_event(
                            event_type="ingestion.snapshot-reused",
                            entity_type="snapshot",
                            entity_id=existing.id,
                            actor=actor,
                            correlation_id=correlation_id,
                            details=self._audit_details(prepared, reused=True, snapshot_id=existing.id),
                            occurred_at=prepared.source_observed_at_utc,
                        )
                        return self._from_record(existing, prepared=prepared, reused=True)

                    if existing is not None and existing.status is SnapshotStatus.BUILDING:
                        return self._recover_building(
                            snapshots=snapshots,
                            audit=audit,
                            existing_id=existing.id,
                            prepared=prepared,
                            final_directory=final_directory,
                            actor=actor,
                            correlation_id=correlation_id,
                        )

                    if final_directory.exists():
                        return self._adopt_orphan_or_conflict(
                            snapshots=snapshots,
                            audit=audit,
                            prepared=prepared,
                            final_directory=final_directory,
                            actor=actor,
                            correlation_id=correlation_id,
                        )

                    record = snapshots.create_building_snapshot(
                        snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                        relative_path=prepared.relative_manifest_path,
                        source_name=prepared.source_name,
                        schema_version=prepared.schema_version,
                        metadata=prepared.metadata,
                        snapshot_id=prepared.snapshot_id,
                        source_version=prepared.source_version,
                        created_at=prepared.source_observed_at_utc,
                    )
                    try:
                        final_directory.parent.mkdir(parents=True, exist_ok=True)
                        prepared.temporary_directory.rename(final_directory)
                    except OSError as exc:
                        msg = "failed to publish snapshot directory"
                        raise SnapshotIntegrityError(msg) from exc
                    ready = snapshots.mark_snapshot_ready(
                        record.id,
                        checksum_sha256=prepared.manifest_checksum_sha256,
                        row_count=prepared.games_count,
                        expected_version=record.version,
                        ready_at=prepared.source_observed_at_utc,
                    )
                    audit.append_event(
                        event_type="ingestion.snapshot-created",
                        entity_type="snapshot",
                        entity_id=ready.id,
                        actor=actor,
                        correlation_id=correlation_id,
                        details=self._audit_details(prepared, reused=False, snapshot_id=ready.id),
                        occurred_at=prepared.source_observed_at_utc,
                    )
                    return self._from_record(ready, prepared=prepared, reused=False)
        except DatabaseIntegrityError as exc:
            # Concurrent insert of the same active source version.
            discard_prepared_snapshot(prepared)
            with connect_database(self._database_path, read_only=True) as connection:
                snapshots = SnapshotRepository(connection)
                existing = snapshots.get_active_snapshot_by_source_version(
                    snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                    source_name=prepared.source_name,
                    source_version=prepared.source_version,
                    schema_version=prepared.schema_version,
                )
            if existing is not None and existing.status is SnapshotStatus.READY:
                verify_snapshot_directory(
                    snapshots_directory=self._snapshots_directory,
                    relative_manifest_path=existing.relative_path,
                    expected_snapshot=existing,
                )
                return self._from_record(existing, prepared=prepared, reused=True)
            if existing is not None and existing.status is SnapshotStatus.BUILDING:
                msg = "active snapshot build in progress for source version"
                raise SnapshotBusyError(msg) from exc
            raise

    def _recover_building(
        self,
        *,
        snapshots: SnapshotRepository,
        audit: AuditEventRepository,
        existing_id: str,
        prepared: PreparedSnapshot,
        final_directory: Path,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot:
        existing = snapshots.get_snapshot(existing_id)
        if existing is None:
            msg = "building snapshot disappeared during publication"
            raise SnapshotIntegrityError(msg)
        if final_directory.exists():
            # Complete the READY transition using the already-published directory.
            discard_prepared_snapshot(prepared)
            verified = verify_snapshot_directory(
                snapshots_directory=self._snapshots_directory,
                relative_manifest_path=existing.relative_path,
            )
            ready = snapshots.mark_snapshot_ready(
                existing.id,
                checksum_sha256=verified.manifest_checksum_sha256,
                row_count=verified.games_count,
                expected_version=existing.version,
                ready_at=prepared.source_observed_at_utc,
            )
            audit.append_event(
                event_type="ingestion.snapshot-created",
                entity_type="snapshot",
                entity_id=ready.id,
                actor=actor,
                correlation_id=correlation_id,
                details=self._audit_details(prepared, reused=False, snapshot_id=ready.id),
                occurred_at=prepared.source_observed_at_utc,
            )
            return self._from_record(ready, prepared=prepared, reused=False)
        discard_prepared_snapshot(prepared)
        msg = (
            "incomplete snapshot publication: BUILDING metadata exists without a "
            "final directory; retry later"
        )
        raise SnapshotBusyError(msg)

    def _adopt_orphan_or_conflict(
        self,
        *,
        snapshots: SnapshotRepository,
        audit: AuditEventRepository,
        prepared: PreparedSnapshot,
        final_directory: Path,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot:
        # Final directory exists without an active row: adopt if identity matches.
        try:
            verified = verify_snapshot_directory(
                snapshots_directory=self._snapshots_directory,
                relative_manifest_path=prepared.relative_manifest_path,
            )
        except Exception as exc:
            discard_prepared_snapshot(prepared)
            msg = (
                f"conflicting snapshot directory at {prepared.relative_directory}; "
                "refusing to overwrite"
            )
            raise SnapshotIntegrityError(msg) from exc
        if verified.manifest_checksum_sha256 != prepared.manifest_checksum_sha256:
            # Still adopt the on-disk immutable directory if identity fields match.
            pass
        discard_prepared_snapshot(prepared)
        record = snapshots.create_building_snapshot(
            snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
            relative_path=prepared.relative_manifest_path,
            source_name=prepared.source_name,
            schema_version=prepared.schema_version,
            metadata=prepared.metadata,
            snapshot_id=verified.snapshot_id,
            source_version=prepared.source_version,
            created_at=prepared.source_observed_at_utc,
        )
        ready = snapshots.mark_snapshot_ready(
            record.id,
            checksum_sha256=verified.manifest_checksum_sha256,
            row_count=verified.games_count,
            expected_version=record.version,
            ready_at=prepared.source_observed_at_utc,
        )
        audit.append_event(
            event_type="ingestion.snapshot-created",
            entity_type="snapshot",
            entity_id=ready.id,
            actor=actor,
            correlation_id=correlation_id,
            details=self._audit_details(prepared, reused=False, snapshot_id=ready.id),
            occurred_at=prepared.source_observed_at_utc,
        )
        return self._from_record(ready, prepared=prepared, reused=False)

    @staticmethod
    def _audit_details(
        prepared: PreparedSnapshot,
        *,
        reused: bool,
        snapshot_id: str,
    ) -> dict[str, JsonValue]:
        return {
            "snapshot_id": snapshot_id,
            "source_name": prepared.source_name,
            "source_version": prepared.source_version,
            "competition_id": prepared.competition_id,
            "season_id": prepared.season_id,
            "games_count": prepared.games_count,
            "odds_count": prepared.odds_quotes_count,
            "statistics_count": prepared.statistics_rows_count,
            "reused": reused,
        }

    @staticmethod
    def _from_record(
        record: object,
        *,
        prepared: PreparedSnapshot,
        reused: bool,
    ) -> PublishedSnapshot:
        from sports_analytics.data.types import SnapshotRecord

        assert isinstance(record, SnapshotRecord)
        return PublishedSnapshot(
            snapshot_id=record.id,
            snapshot_status=record.status,
            snapshot_reused=reused,
            snapshot_relative_path=record.relative_path,
            source_name=record.source_name,
            source_version=record.source_version or prepared.source_version,
            source_file_sha256=prepared.source_file_sha256,
            competition_id=prepared.competition_id,
            season_id=prepared.season_id,
            games_count=record.row_count if record.row_count is not None else prepared.games_count,
            teams_count=prepared.teams_count,
            odds_quotes_count=prepared.odds_quotes_count,
            statistics_rows_count=prepared.statistics_rows_count,
            duplicate_rows_discarded=prepared.duplicate_rows_discarded,
            warnings_count=prepared.warnings_count,
            manifest_checksum_sha256=record.checksum_sha256
            or prepared.manifest_checksum_sha256,
            source_observed_at_utc=prepared.source_observed_at_utc,
        )


def cleanup_temp_directory(path: Path) -> None:
    """Best-effort temporary directory cleanup that preserves primary exceptions."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass

"""Snapshot metadata repository."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
    loads_canonical_json,
    parse_utc_timestamp,
    utc_now,
)
from sports_analytics.data.database import require_active_transaction
from sports_analytics.data.schema import SNAPSHOTS_TABLE
from sports_analytics.data.types import (
    JsonValue,
    SnapshotRecord,
    SnapshotStatus,
    normalize_uuid,
    validate_identifier,
    validate_limit_offset,
    validate_relative_snapshot_path,
    validate_sha256_checksum,
    validate_strict_int,
)


class SnapshotRepository:
    """Typed snapshot metadata repository. Does not create physical files."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_building_snapshot(
        self,
        *,
        snapshot_type: str,
        relative_path: str,
        source_name: str,
        schema_version: str,
        metadata: JsonValue,
        snapshot_id: str | uuid.UUID | None = None,
        source_version: str | None = None,
        created_at: datetime | None = None,
    ) -> SnapshotRecord:
        """Create a building snapshot metadata row."""
        require_active_transaction(
            self._connection,
            operation="SnapshotRepository.create_building_snapshot",
        )
        normalized_type = validate_identifier(snapshot_type, field_name="snapshot_type")
        normalized_source = validate_identifier(source_name, field_name="source_name")
        normalized_schema = validate_identifier(schema_version, field_name="schema_version")
        if source_version is not None:
            source_version = validate_identifier(source_version, field_name="source_version")
        path = validate_relative_snapshot_path(relative_path)
        normalized_id = normalize_uuid(snapshot_id)
        created = created_at if created_at is not None else utc_now()
        metadata_json = dumps_canonical_json(ensure_json_value(metadata))
        try:
            self._connection.execute(
                f"""
                INSERT INTO {SNAPSHOTS_TABLE} (
                    id, snapshot_type, status, relative_path, checksum_sha256,
                    row_count, source_name, source_version, schema_version,
                    created_at, ready_at, metadata_json, version
                ) VALUES (
                    ?, ?, ?, ?, NULL,
                    NULL, ?, ?, ?,
                    ?, NULL, ?, 1
                )
                """,
                (
                    normalized_id,
                    normalized_type,
                    SnapshotStatus.BUILDING.value,
                    path,
                    normalized_source,
                    source_version,
                    normalized_schema,
                    format_utc_timestamp(created),
                    metadata_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error creating snapshot {normalized_id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to create snapshot {normalized_id}"
            raise RepositoryError(msg) from exc
        record = self.get_snapshot(normalized_id)
        if record is None:
            msg = f"snapshot {normalized_id} was not readable after insert"
            raise RepositoryError(msg)
        return record

    def get_snapshot(self, snapshot_id: str | uuid.UUID) -> SnapshotRecord | None:
        """Return one snapshot metadata record."""
        normalized_id = normalize_uuid(snapshot_id)
        try:
            row = self._connection.execute(
                f"SELECT * FROM {SNAPSHOTS_TABLE} WHERE id = ?",
                (normalized_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = f"failed to read snapshot {normalized_id}"
            raise RepositoryError(msg) from exc
        if row is None:
            return None
        return self._from_row(row)

    def list_snapshots(
        self,
        *,
        snapshot_type: str | None = None,
        status: SnapshotStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SnapshotRecord]:
        """List snapshots with deterministic filters and ordering."""
        limit, offset = validate_limit_offset(limit=limit, offset=offset)
        clauses: list[str] = []
        params: list[object] = []
        if snapshot_type is not None:
            clauses.append("snapshot_type = ?")
            params.append(validate_identifier(snapshot_type, field_name="snapshot_type"))
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM {SNAPSHOTS_TABLE} {where} ORDER BY created_at DESC, id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to list snapshots"
            raise RepositoryError(msg) from exc
        return [self._from_row(row) for row in rows]

    def mark_snapshot_ready(
        self,
        snapshot_id: str | uuid.UUID,
        *,
        checksum_sha256: str,
        row_count: int,
        expected_version: int,
        ready_at: datetime | None = None,
    ) -> SnapshotRecord:
        """Mark a building snapshot as immutable ready metadata."""
        require_active_transaction(
            self._connection,
            operation="SnapshotRepository.mark_snapshot_ready",
        )
        normalized_id = normalize_uuid(snapshot_id)
        checksum = validate_sha256_checksum(checksum_sha256)
        row_count = validate_strict_int(row_count, field_name="row_count", minimum=0)
        expected_version = validate_strict_int(
            expected_version,
            field_name="expected_version",
            minimum=1,
        )
        current = self.get_snapshot(normalized_id)
        if current is None:
            msg = f"snapshot not found: {normalized_id}"
            raise RepositoryError(msg)
        if current.status is SnapshotStatus.READY:
            msg = f"ready snapshot {normalized_id} is immutable"
            raise RepositoryError(msg)
        if current.status is not SnapshotStatus.BUILDING:
            msg = (
                f"snapshot {normalized_id} must be building to mark ready, "
                f"found {current.status.value}"
            )
            raise RepositoryError(msg)
        if current.version != expected_version:
            msg = (
                f"snapshot {normalized_id} expected version={expected_version}, "
                f"found {current.version}"
            )
            raise DatabaseIntegrityError(msg)
        timestamp = ready_at if ready_at is not None else utc_now()
        new_version = current.version + 1
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {SNAPSHOTS_TABLE}
                SET status = ?,
                    checksum_sha256 = ?,
                    row_count = ?,
                    ready_at = ?,
                    version = ?
                WHERE id = ? AND status = ? AND version = ?
                """,
                (
                    SnapshotStatus.READY.value,
                    checksum,
                    row_count,
                    format_utc_timestamp(timestamp),
                    new_version,
                    normalized_id,
                    SnapshotStatus.BUILDING.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                msg = f"snapshot {normalized_id} ready transition failed"
                raise DatabaseIntegrityError(msg)
        except DatabaseIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error marking snapshot {normalized_id} ready"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to mark snapshot {normalized_id} ready"
            raise RepositoryError(msg) from exc
        updated = self.get_snapshot(normalized_id)
        if updated is None:
            msg = f"snapshot {normalized_id} missing after ready transition"
            raise RepositoryError(msg)
        return updated

    def mark_snapshot_failed(
        self,
        snapshot_id: str | uuid.UUID,
        *,
        expected_version: int,
        metadata: JsonValue | None = None,
    ) -> SnapshotRecord:
        """Mark a building snapshot as failed without deleting files."""
        require_active_transaction(
            self._connection,
            operation="SnapshotRepository.mark_snapshot_failed",
        )
        normalized_id = normalize_uuid(snapshot_id)
        expected_version = validate_strict_int(
            expected_version,
            field_name="expected_version",
            minimum=1,
        )
        current = self.get_snapshot(normalized_id)
        if current is None:
            msg = f"snapshot not found: {normalized_id}"
            raise RepositoryError(msg)
        if current.status is SnapshotStatus.READY:
            msg = f"ready snapshot {normalized_id} is immutable"
            raise RepositoryError(msg)
        if current.status is not SnapshotStatus.BUILDING:
            msg = (
                f"snapshot {normalized_id} must be building to mark failed, "
                f"found {current.status.value}"
            )
            raise RepositoryError(msg)
        if current.version != expected_version:
            msg = (
                f"snapshot {normalized_id} expected version={expected_version}, "
                f"found {current.version}"
            )
            raise DatabaseIntegrityError(msg)
        metadata_json = (
            dumps_canonical_json(ensure_json_value(metadata))
            if metadata is not None
            else dumps_canonical_json(current.metadata)
        )
        new_version = current.version + 1
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {SNAPSHOTS_TABLE}
                SET status = ?,
                    ready_at = NULL,
                    metadata_json = ?,
                    version = ?
                WHERE id = ? AND status = ? AND version = ?
                """,
                (
                    SnapshotStatus.FAILED.value,
                    metadata_json,
                    new_version,
                    normalized_id,
                    SnapshotStatus.BUILDING.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                msg = f"snapshot {normalized_id} failed transition failed"
                raise DatabaseIntegrityError(msg)
        except DatabaseIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error marking snapshot {normalized_id} failed"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to mark snapshot {normalized_id} failed"
            raise RepositoryError(msg) from exc
        updated = self.get_snapshot(normalized_id)
        if updated is None:
            msg = f"snapshot {normalized_id} missing after failed transition"
            raise RepositoryError(msg)
        return updated

    def _from_row(self, row: sqlite3.Row) -> SnapshotRecord:
        try:
            metadata = loads_canonical_json(str(row["metadata_json"]))
        except RepositoryError as exc:
            msg = f"malformed metadata_json for snapshot {row['id']}"
            raise RepositoryError(msg) from exc
        return SnapshotRecord(
            id=str(row["id"]),
            snapshot_type=str(row["snapshot_type"]),
            status=SnapshotStatus(str(row["status"])),
            relative_path=str(row["relative_path"]),
            checksum_sha256=(
                None if row["checksum_sha256"] is None else str(row["checksum_sha256"])
            ),
            row_count=None if row["row_count"] is None else int(row["row_count"]),
            source_name=str(row["source_name"]),
            source_version=(None if row["source_version"] is None else str(row["source_version"])),
            schema_version=str(row["schema_version"]),
            created_at=parse_utc_timestamp(str(row["created_at"])),
            ready_at=(
                None if row["ready_at"] is None else parse_utc_timestamp(str(row["ready_at"]))
            ),
            metadata=metadata,
            version=int(row["version"]),
        )

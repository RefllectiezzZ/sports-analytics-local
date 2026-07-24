"""Append-only audit event repository."""

from __future__ import annotations

import sqlite3
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
from sports_analytics.data.schema import AUDIT_EVENTS_TABLE
from sports_analytics.data.types import (
    AuditEventRecord,
    JsonValue,
    validate_identifier,
    validate_limit_offset,
    validate_strict_int,
)


class AuditEventRepository:
    """Typed append-only audit trail repository."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def append_event(
        self,
        *,
        event_type: str,
        entity_type: str,
        actor: str,
        details: JsonValue,
        entity_id: str | None = None,
        occurred_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AuditEventRecord:
        """Append one audit event and return the stored record."""
        require_active_transaction(self._connection, operation="AuditEventRepository.append_event")
        normalized_event_type = validate_identifier(event_type, field_name="event_type")
        normalized_entity_type = validate_identifier(entity_type, field_name="entity_type")
        normalized_actor = validate_identifier(actor, field_name="actor")
        if entity_id is not None:
            entity_id = entity_id if entity_id else None
            if entity_id is not None and entity_id != entity_id.strip():
                msg = "entity_id must not have leading or trailing whitespace"
                raise RepositoryError(msg)
        if correlation_id is not None:
            correlation_id = validate_identifier(correlation_id, field_name="correlation_id")
        timestamp = occurred_at if occurred_at is not None else utc_now()
        details_json = dumps_canonical_json(ensure_json_value(details))
        try:
            cursor = self._connection.execute(
                f"""
                INSERT INTO {AUDIT_EVENTS_TABLE} (
                    event_type, entity_type, entity_id, actor, occurred_at,
                    correlation_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_event_type,
                    normalized_entity_type,
                    entity_id,
                    normalized_actor,
                    format_utc_timestamp(timestamp),
                    correlation_id,
                    details_json,
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                msg = "audit event insert did not return a row id"
                raise RepositoryError(msg)
            event_id = int(event_id)
        except sqlite3.IntegrityError as exc:
            msg = "integrity error appending audit event"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = "failed to append audit event"
            raise RepositoryError(msg) from exc
        record = self.get_event(event_id)
        if record is None:
            msg = f"audit event {event_id} was not readable after insert"
            raise RepositoryError(msg)
        return record

    def get_event(self, event_id: object) -> AuditEventRecord | None:
        """Return one audit event by id."""
        validated_id = validate_strict_int(event_id, field_name="event_id", minimum=1)
        try:
            row = self._connection.execute(
                f"SELECT * FROM {AUDIT_EVENTS_TABLE} WHERE id = ?",
                (validated_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = f"failed to read audit event {validated_id}"
            raise RepositoryError(msg) from exc
        if row is None:
            return None
        return self._row_to_record(row)

    def list_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        correlation_id: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditEventRecord]:
        """List audit events with deterministic filters and ordering."""
        limit, offset = validate_limit_offset(limit=limit, offset=offset)
        clauses: list[str] = []
        params: list[object] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(validate_identifier(event_type, field_name="event_type"))
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(validate_identifier(entity_type, field_name="entity_type"))
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(validate_identifier(correlation_id, field_name="correlation_id"))
        if occurred_from is not None:
            clauses.append("occurred_at >= ?")
            params.append(format_utc_timestamp(occurred_from))
        if occurred_to is not None:
            clauses.append("occurred_at <= ?")
            params.append(format_utc_timestamp(occurred_to))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM {AUDIT_EVENTS_TABLE} {where} ORDER BY occurred_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to list audit events"
            raise RepositoryError(msg) from exc
        return [self._row_to_record(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> AuditEventRecord:
        try:
            details = loads_canonical_json(str(row["details_json"]))
        except RepositoryError as exc:
            msg = f"malformed details_json for audit event {row['id']}"
            raise RepositoryError(msg) from exc
        return AuditEventRecord(
            id=int(row["id"]),
            event_type=str(row["event_type"]),
            entity_type=str(row["entity_type"]),
            entity_id=None if row["entity_id"] is None else str(row["entity_id"]),
            actor=str(row["actor"]),
            occurred_at=parse_utc_timestamp(str(row["occurred_at"])),
            correlation_id=(None if row["correlation_id"] is None else str(row["correlation_id"])),
            details=details,
        )

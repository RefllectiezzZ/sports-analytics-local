"""Durable job and job-event repository foundation."""

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
from sports_analytics.data.schema import JOB_EVENTS_TABLE, JOBS_TABLE
from sports_analytics.data.types import (
    DEFAULT_JOB_PRIORITY,
    JobEventRecord,
    JobRecord,
    JobStatus,
    JsonValue,
    normalize_uuid,
    validate_identifier,
    validate_limit_offset,
    validate_plain_text,
)

_ALLOWED_TRANSITIONS: dict[tuple[JobStatus, JobStatus], str] = {
    (JobStatus.PENDING, JobStatus.RUNNING): "started",
    (JobStatus.PENDING, JobStatus.CANCELLED): "cancelled",
    (JobStatus.RUNNING, JobStatus.SUCCEEDED): "succeeded",
    (JobStatus.RUNNING, JobStatus.FAILED): "failed",
    (JobStatus.RUNNING, JobStatus.PENDING): "retry_requested",
    (JobStatus.FAILED, JobStatus.PENDING): "retry_requested",
    (JobStatus.FAILED, JobStatus.CANCELLED): "cancelled",
}


class JobRepository:
    """Typed job repository. Does not own connections or transactions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_job(
        self,
        *,
        job_type: str,
        payload: JsonValue,
        maximum_attempts: int,
        actor: str,
        job_id: str | uuid.UUID | None = None,
        priority: int = DEFAULT_JOB_PRIORITY,
        available_at: datetime | None = None,
        created_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        """Create a pending job and its initial event in the caller transaction."""
        if maximum_attempts <= 0:
            msg = "maximum_attempts must be > 0"
            raise RepositoryError(msg)
        if priority != int(priority):
            msg = "priority must be an integer"
            raise RepositoryError(msg)
        normalized_type = validate_identifier(job_type, field_name="job_type")
        normalized_actor = validate_identifier(actor, field_name="actor")
        if idempotency_key is not None:
            idempotency_key = validate_plain_text(
                idempotency_key,
                field_name="idempotency_key",
            )
            if not idempotency_key.strip() or idempotency_key != idempotency_key.strip():
                msg = "idempotency_key must be non-empty without surrounding whitespace"
                raise RepositoryError(msg)
        canonical_payload = ensure_json_value(payload)
        payload_json = dumps_canonical_json(canonical_payload)
        created = created_at if created_at is not None else utc_now()
        available = available_at if available_at is not None else created
        normalized_id = normalize_uuid(job_id)

        if idempotency_key is not None:
            existing = self.get_job_by_idempotency_key(idempotency_key)
            if existing is not None:
                existing_payload = dumps_canonical_json(existing.payload)
                if existing.job_type != normalized_type or existing_payload != payload_json:
                    msg = (
                        "idempotency_key conflicts with an existing job of different "
                        "type or payload"
                    )
                    raise DatabaseIntegrityError(msg)
                return existing

        try:
            self._connection.execute(
                f"""
                INSERT INTO {JOBS_TABLE} (
                    id, job_type, payload_json, status, priority, attempts,
                    maximum_attempts, available_at, lease_owner, lease_expires_at,
                    created_at, updated_at, started_at, finished_at, last_error,
                    idempotency_key, result_json, version
                ) VALUES (
                    ?, ?, ?, ?, ?, 0,
                    ?, ?, NULL, NULL,
                    ?, ?, NULL, NULL, NULL,
                    ?, NULL, 1
                )
                """,
                (
                    normalized_id,
                    normalized_type,
                    payload_json,
                    JobStatus.PENDING.value,
                    int(priority),
                    maximum_attempts,
                    format_utc_timestamp(available),
                    format_utc_timestamp(created),
                    format_utc_timestamp(created),
                    idempotency_key,
                ),
            )
            self._connection.execute(
                f"""
                INSERT INTO {JOB_EVENTS_TABLE} (
                    job_id, event_type, from_status, to_status, details_json,
                    occurred_at, actor, job_version
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, 1)
                """,
                (
                    normalized_id,
                    "created",
                    JobStatus.PENDING.value,
                    dumps_canonical_json({"job_type": normalized_type}),
                    format_utc_timestamp(created),
                    normalized_actor,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if idempotency_key is not None:
                existing = self.get_job_by_idempotency_key(idempotency_key)
                if existing is not None:
                    existing_payload = dumps_canonical_json(existing.payload)
                    if existing.job_type == normalized_type and existing_payload == payload_json:
                        return existing
                    msg = (
                        "idempotency_key conflicts with an existing job of different "
                        "type or payload"
                    )
                    raise DatabaseIntegrityError(msg) from exc
            msg = f"integrity error creating job {normalized_id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to create job {normalized_id}"
            raise RepositoryError(msg) from exc

        job = self.get_job(normalized_id)
        if job is None:
            msg = f"job {normalized_id} was not readable after insert"
            raise RepositoryError(msg)
        return job

    def get_job(self, job_id: str | uuid.UUID) -> JobRecord | None:
        """Return one job by canonical UUID."""
        normalized_id = normalize_uuid(job_id)
        try:
            row = self._connection.execute(
                f"SELECT * FROM {JOBS_TABLE} WHERE id = ?",
                (normalized_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = f"failed to read job {normalized_id}"
            raise RepositoryError(msg) from exc
        if row is None:
            return None
        return self._job_from_row(row)

    def get_job_by_idempotency_key(self, idempotency_key: str) -> JobRecord | None:
        """Return a job with the given non-NULL idempotency key."""
        key = validate_plain_text(idempotency_key, field_name="idempotency_key")
        try:
            row = self._connection.execute(
                f"SELECT * FROM {JOBS_TABLE} WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = "failed to read job by idempotency key"
            raise RepositoryError(msg) from exc
        if row is None:
            return None
        return self._job_from_row(row)

    def list_jobs(
        self,
        *,
        status: JobStatus | None = None,
        job_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[JobRecord]:
        """List jobs with deterministic filters and ordering."""
        limit, offset = validate_limit_offset(limit=limit, offset=offset)
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if job_type is not None:
            clauses.append("job_type = ?")
            params.append(validate_identifier(job_type, field_name="job_type"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM {JOBS_TABLE} {where} ORDER BY created_at DESC, id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to list jobs"
            raise RepositoryError(msg) from exc
        return [self._job_from_row(row) for row in rows]

    def count_jobs(
        self,
        *,
        status: JobStatus | None = None,
        job_type: str | None = None,
    ) -> int:
        """Count jobs matching optional filters."""
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if job_type is not None:
            clauses.append("job_type = ?")
            params.append(validate_identifier(job_type, field_name="job_type"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            row = self._connection.execute(
                f"SELECT COUNT(*) AS count FROM {JOBS_TABLE} {where}",
                params,
            ).fetchone()
        except sqlite3.Error as exc:
            msg = "failed to count jobs"
            raise RepositoryError(msg) from exc
        return int(row["count"]) if row is not None else 0

    def list_job_events(self, job_id: str | uuid.UUID) -> list[JobEventRecord]:
        """Return append-only events for a job ordered by occurrence."""
        normalized_id = normalize_uuid(job_id)
        try:
            rows = self._connection.execute(
                f"""
                SELECT * FROM {JOB_EVENTS_TABLE}
                WHERE job_id = ?
                ORDER BY occurred_at ASC, id ASC
                """,
                (normalized_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            msg = f"failed to list events for job {normalized_id}"
            raise RepositoryError(msg) from exc
        return [self._event_from_row(row) for row in rows]

    def transition_job(
        self,
        job_id: str | uuid.UUID,
        *,
        expected_status: JobStatus,
        expected_version: int,
        new_status: JobStatus,
        actor: str,
        occurred_at: datetime | None = None,
        result: JsonValue | None = None,
        last_error: str | None = None,
        details: JsonValue | None = None,
    ) -> JobRecord:
        """Apply an allowed status transition with optimistic version checks."""
        normalized_id = normalize_uuid(job_id)
        normalized_actor = validate_identifier(actor, field_name="actor")
        transition_key = (expected_status, new_status)
        if transition_key not in _ALLOWED_TRANSITIONS:
            msg = f"disallowed job transition {expected_status.value} -> {new_status.value}"
            raise RepositoryError(msg)
        if result is not None and new_status is not JobStatus.SUCCEEDED:
            msg = "result JSON is only accepted when transitioning to succeeded"
            raise RepositoryError(msg)
        if last_error is not None and new_status is not JobStatus.FAILED:
            msg = "last_error is only accepted when transitioning to failed"
            raise RepositoryError(msg)
        if new_status is JobStatus.SUCCEEDED and result is None:
            msg = "succeeded jobs require result JSON"
            raise RepositoryError(msg)
        if new_status is JobStatus.FAILED and last_error is None:
            msg = "failed jobs require last_error"
            raise RepositoryError(msg)

        current = self.get_job(normalized_id)
        if current is None:
            msg = f"job not found: {normalized_id}"
            raise RepositoryError(msg)
        if current.status is not expected_status or current.version != expected_version:
            msg = (
                f"job {normalized_id} expected status={expected_status.value} "
                f"version={expected_version}, found status={current.status.value} "
                f"version={current.version}"
            )
            raise DatabaseIntegrityError(msg)

        if (
            expected_status is JobStatus.FAILED
            and new_status is JobStatus.PENDING
            and current.attempts >= current.maximum_attempts
        ):
            msg = "cannot retry failed job when attempts have reached maximum_attempts"
            raise RepositoryError(msg)

        timestamp = occurred_at if occurred_at is not None else utc_now()
        new_version = current.version + 1
        started_at = current.started_at
        finished_at = current.finished_at
        attempts = current.attempts
        if result is not None:
            result_json = dumps_canonical_json(ensure_json_value(result))
        else:
            result_json = None
        error_text = (
            validate_plain_text(last_error, field_name="last_error")
            if last_error is not None
            else current.last_error
        )

        if new_status is JobStatus.RUNNING:
            started_at = timestamp
            finished_at = None
            attempts = current.attempts + 1
            error_text = None
            result_json = None
        elif new_status is JobStatus.SUCCEEDED:
            finished_at = timestamp
            if started_at is None:
                started_at = timestamp
        elif new_status is JobStatus.FAILED:
            finished_at = timestamp
            if started_at is None:
                started_at = timestamp
        elif new_status is JobStatus.CANCELLED:
            finished_at = timestamp
        elif new_status is JobStatus.PENDING:
            finished_at = None
            # Retry clears lease and leaves attempts unchanged until next start.
            error_text = current.last_error if expected_status is JobStatus.FAILED else None
            result_json = None

        event_type = _ALLOWED_TRANSITIONS[transition_key]
        event_details = details if details is not None else {}
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET status = ?,
                    attempts = ?,
                    updated_at = ?,
                    started_at = ?,
                    finished_at = ?,
                    last_error = ?,
                    result_json = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    version = ?
                WHERE id = ? AND status = ? AND version = ?
                """,
                (
                    new_status.value,
                    attempts,
                    format_utc_timestamp(timestamp),
                    None if started_at is None else format_utc_timestamp(started_at),
                    None if finished_at is None else format_utc_timestamp(finished_at),
                    error_text,
                    result_json if new_status is JobStatus.SUCCEEDED else None,
                    new_version,
                    normalized_id,
                    expected_status.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                msg = f"job {normalized_id} transition failed due to concurrent modification"
                raise DatabaseIntegrityError(msg)
            self._connection.execute(
                f"""
                INSERT INTO {JOB_EVENTS_TABLE} (
                    job_id, event_type, from_status, to_status, details_json,
                    occurred_at, actor, job_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    event_type,
                    expected_status.value,
                    new_status.value,
                    dumps_canonical_json(ensure_json_value(event_details)),
                    format_utc_timestamp(timestamp),
                    normalized_actor,
                    new_version,
                ),
            )
        except DatabaseIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error transitioning job {normalized_id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to transition job {normalized_id}"
            raise RepositoryError(msg) from exc

        updated = self.get_job(normalized_id)
        if updated is None:
            msg = f"job {normalized_id} missing after transition"
            raise RepositoryError(msg)
        return updated

    def _job_from_row(self, row: sqlite3.Row) -> JobRecord:
        try:
            payload = loads_canonical_json(str(row["payload_json"]))
            result = (
                None
                if row["result_json"] is None
                else loads_canonical_json(str(row["result_json"]))
            )
        except RepositoryError as exc:
            msg = f"malformed JSON for job {row['id']}"
            raise RepositoryError(msg) from exc
        return JobRecord(
            id=str(row["id"]),
            job_type=str(row["job_type"]),
            payload=payload,
            status=JobStatus(str(row["status"])),
            priority=int(row["priority"]),
            attempts=int(row["attempts"]),
            maximum_attempts=int(row["maximum_attempts"]),
            available_at=parse_utc_timestamp(str(row["available_at"])),
            lease_owner=None if row["lease_owner"] is None else str(row["lease_owner"]),
            lease_expires_at=(
                None
                if row["lease_expires_at"] is None
                else parse_utc_timestamp(str(row["lease_expires_at"]))
            ),
            created_at=parse_utc_timestamp(str(row["created_at"])),
            updated_at=parse_utc_timestamp(str(row["updated_at"])),
            started_at=(
                None if row["started_at"] is None else parse_utc_timestamp(str(row["started_at"]))
            ),
            finished_at=(
                None if row["finished_at"] is None else parse_utc_timestamp(str(row["finished_at"]))
            ),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            idempotency_key=(
                None if row["idempotency_key"] is None else str(row["idempotency_key"])
            ),
            result=result,
            version=int(row["version"]),
        )

    def _event_from_row(self, row: sqlite3.Row) -> JobEventRecord:
        try:
            details = loads_canonical_json(str(row["details_json"]))
        except RepositoryError as exc:
            msg = f"malformed details_json for job event {row['id']}"
            raise RepositoryError(msg) from exc
        return JobEventRecord(
            id=int(row["id"]),
            job_id=str(row["job_id"]),
            event_type=str(row["event_type"]),
            from_status=(
                None if row["from_status"] is None else JobStatus(str(row["from_status"]))
            ),
            to_status=None if row["to_status"] is None else JobStatus(str(row["to_status"])),
            details=details,
            occurred_at=parse_utc_timestamp(str(row["occurred_at"])),
            actor=str(row["actor"]),
            job_version=int(row["job_version"]),
        )

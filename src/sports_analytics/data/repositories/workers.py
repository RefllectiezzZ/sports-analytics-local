"""Durable worker-instance repository."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
    loads_canonical_json,
    parse_utc_timestamp,
)
from sports_analytics.data.database import require_active_transaction
from sports_analytics.data.schema import WORKER_INSTANCES_TABLE
from sports_analytics.data.types import (
    JsonValue,
    normalize_uuid,
    validate_identifier,
    validate_limit_offset,
    validate_plain_text,
    validate_positive_finite_number,
    validate_strict_int,
)
from sports_analytics.jobs.errors import sanitize_error_text
from sports_analytics.jobs.types import (
    StaleWorkerReconciliationResult,
    WorkerRecord,
    WorkerStatus,
)

_ACTIVE_HEARTBEAT_STATUSES = frozenset(
    {
        WorkerStatus.STARTING,
        WorkerStatus.RUNNING,
        WorkerStatus.STOPPING,
    }
)
_TERMINAL_STATUSES = frozenset({WorkerStatus.STOPPED, WorkerStatus.FAILED})


class WorkerRepository:
    """Typed worker-instance repository. Does not own connections or transactions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def register_worker(
        self,
        *,
        name: str,
        process_id: int,
        hostname: str,
        started_at: datetime,
        capabilities: JsonValue,
        worker_id: str | uuid.UUID | None = None,
        status: WorkerStatus = WorkerStatus.STARTING,
        heartbeat_at: datetime | None = None,
    ) -> WorkerRecord:
        """Insert a worker instance with version=1.

        Typical lifecycle registers ``starting`` first, then transitions to
        ``running`` after initialization succeeds.
        """
        require_active_transaction(self._connection, operation="WorkerRepository.register_worker")
        if status not in {WorkerStatus.STARTING, WorkerStatus.RUNNING}:
            msg = "register_worker status must be starting or running"
            raise RepositoryError(msg)
        normalized_id = normalize_uuid(worker_id)
        normalized_name = validate_plain_text(name, field_name="name").strip()
        if not normalized_name:
            msg = "name must be non-empty"
            raise RepositoryError(msg)
        process_id = validate_strict_int(process_id, field_name="process_id", minimum=1)
        hostname_text = validate_plain_text(hostname, field_name="hostname").strip()
        if not hostname_text:
            msg = "hostname must be non-empty"
            raise RepositoryError(msg)
        if started_at.tzinfo is None:
            msg = "started_at must be timezone-aware"
            raise RepositoryError(msg)
        heartbeat = heartbeat_at if heartbeat_at is not None else started_at
        if heartbeat.tzinfo is None:
            msg = "heartbeat_at must be timezone-aware"
            raise RepositoryError(msg)
        capabilities_json = dumps_canonical_json(ensure_json_value(capabilities))
        if not capabilities_json:
            msg = "capabilities_json must be non-empty"
            raise RepositoryError(msg)
        try:
            self._connection.execute(
                f"""
                INSERT INTO {WORKER_INSTANCES_TABLE} (
                    id, name, status, process_id, hostname, started_at, heartbeat_at,
                    stopping_at, stopped_at, current_job_id, last_error,
                    capabilities_json, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, 1)
                """,
                (
                    normalized_id,
                    normalized_name,
                    status.value,
                    process_id,
                    hostname_text,
                    format_utc_timestamp(started_at),
                    format_utc_timestamp(heartbeat),
                    capabilities_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            msg = f"worker {normalized_id} already exists or violates constraints"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to register worker {normalized_id}"
            raise RepositoryError(msg) from exc
        worker = self.get_worker(normalized_id)
        if worker is None:
            msg = f"worker {normalized_id} was not readable after insert"
            raise RepositoryError(msg)
        return worker

    def get_worker(self, worker_id: str | uuid.UUID) -> WorkerRecord | None:
        """Return one worker by canonical UUID."""
        normalized_id = normalize_uuid(worker_id)
        try:
            row = self._connection.execute(
                f"SELECT * FROM {WORKER_INSTANCES_TABLE} WHERE id = ?",
                (normalized_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = f"failed to read worker {normalized_id}"
            raise RepositoryError(msg) from exc
        if row is None:
            return None
        return self._worker_from_row(row)

    def heartbeat_worker(
        self,
        worker_id: str | uuid.UUID,
        *,
        expected_version: int,
        heartbeat_at: datetime,
        current_job_id: str | uuid.UUID | None = None,
        clear_current_job: bool = False,
    ) -> WorkerRecord:
        """Update heartbeat for a starting, running, or stopping worker."""
        require_active_transaction(self._connection, operation="WorkerRepository.heartbeat_worker")
        expected_version = validate_strict_int(
            expected_version,
            field_name="expected_version",
            minimum=1,
        )
        if heartbeat_at.tzinfo is None:
            msg = "heartbeat_at must be timezone-aware"
            raise RepositoryError(msg)
        if clear_current_job and current_job_id is not None:
            msg = "clear_current_job cannot be combined with current_job_id"
            raise RepositoryError(msg)
        normalized_id = normalize_uuid(worker_id)
        worker = self.get_worker(normalized_id)
        if worker is None:
            msg = f"worker not found: {normalized_id}"
            raise RepositoryError(msg)
        if worker.status not in _ACTIVE_HEARTBEAT_STATUSES:
            msg = f"cannot heartbeat worker in status {worker.status.value}"
            raise RepositoryError(msg)
        if worker.version != expected_version:
            msg = (
                f"worker {normalized_id} expected version={expected_version}, "
                f"found version={worker.version}"
            )
            raise DatabaseIntegrityError(msg)
        if clear_current_job:
            job_id_value: str | None = None
        elif current_job_id is not None:
            job_id_value = normalize_uuid(current_job_id)
        else:
            job_id_value = worker.current_job_id
        new_version = worker.version + 1
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {WORKER_INSTANCES_TABLE}
                SET heartbeat_at = ?,
                    current_job_id = ?,
                    version = ?
                WHERE id = ? AND version = ? AND status IN ('starting', 'running', 'stopping')
                """,
                (
                    format_utc_timestamp(heartbeat_at),
                    job_id_value,
                    new_version,
                    normalized_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                msg = f"worker {normalized_id} heartbeat failed due to concurrent modification"
                raise DatabaseIntegrityError(msg)
        except DatabaseIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error heartbeating worker {normalized_id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to heartbeat worker {normalized_id}"
            raise RepositoryError(msg) from exc
        updated = self.get_worker(normalized_id)
        if updated is None:
            msg = f"worker {normalized_id} missing after heartbeat"
            raise RepositoryError(msg)
        return updated

    def mark_worker_running(
        self,
        worker_id: str | uuid.UUID,
        *,
        expected_version: int,
        heartbeat_at: datetime,
    ) -> WorkerRecord:
        """Transition a starting worker to running."""
        require_active_transaction(
            self._connection,
            operation="WorkerRepository.mark_worker_running",
        )
        return self._transition_status(
            worker_id,
            expected_version=expected_version,
            expected_statuses=(WorkerStatus.STARTING,),
            new_status=WorkerStatus.RUNNING,
            heartbeat_at=heartbeat_at,
            stopping_at=None,
            stopped_at=None,
            clear_current_job=False,
            last_error=None,
            clear_last_error=True,
        )

    def mark_worker_stopping(
        self,
        worker_id: str | uuid.UUID,
        *,
        expected_version: int,
        stopping_at: datetime,
    ) -> WorkerRecord:
        """Transition a running worker to stopping."""
        require_active_transaction(
            self._connection,
            operation="WorkerRepository.mark_worker_stopping",
        )
        return self._transition_status(
            worker_id,
            expected_version=expected_version,
            expected_statuses=(WorkerStatus.RUNNING,),
            new_status=WorkerStatus.STOPPING,
            heartbeat_at=stopping_at,
            stopping_at=stopping_at,
            stopped_at=None,
            clear_current_job=False,
            last_error=None,
            clear_last_error=False,
        )

    def mark_worker_stopped(
        self,
        worker_id: str | uuid.UUID,
        *,
        expected_version: int,
        stopped_at: datetime,
        shutdown_note: str | None = None,
    ) -> WorkerRecord:
        """Transition a non-terminal worker to stopped."""
        require_active_transaction(
            self._connection,
            operation="WorkerRepository.mark_worker_stopped",
        )
        note = sanitize_error_text(shutdown_note) if shutdown_note is not None else None
        return self._transition_status(
            worker_id,
            expected_version=expected_version,
            expected_statuses=(
                WorkerStatus.STARTING,
                WorkerStatus.RUNNING,
                WorkerStatus.STOPPING,
            ),
            new_status=WorkerStatus.STOPPED,
            heartbeat_at=stopped_at,
            stopping_at=None,
            stopped_at=stopped_at,
            clear_current_job=True,
            last_error=note,
            clear_last_error=shutdown_note is None,
        )

    def mark_worker_failed(
        self,
        worker_id: str | uuid.UUID,
        *,
        expected_version: int,
        stopped_at: datetime,
        error: str,
    ) -> WorkerRecord:
        """Transition a non-terminal worker to failed."""
        require_active_transaction(
            self._connection,
            operation="WorkerRepository.mark_worker_failed",
        )
        return self._transition_status(
            worker_id,
            expected_version=expected_version,
            expected_statuses=(
                WorkerStatus.STARTING,
                WorkerStatus.RUNNING,
                WorkerStatus.STOPPING,
            ),
            new_status=WorkerStatus.FAILED,
            heartbeat_at=stopped_at,
            stopping_at=None,
            stopped_at=stopped_at,
            clear_current_job=True,
            last_error=sanitize_error_text(error),
            clear_last_error=False,
        )

    def list_workers(
        self,
        *,
        status: WorkerStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[WorkerRecord]:
        """List workers with deterministic ordering."""
        limit, offset = validate_limit_offset(limit=limit, offset=offset)
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM {WORKER_INSTANCES_TABLE} {where} ORDER BY started_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        try:
            rows = self._connection.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to list workers"
            raise RepositoryError(msg) from exc
        return [self._worker_from_row(row) for row in rows]

    def count_workers(self, *, status: WorkerStatus | None = None) -> int:
        """Count workers matching an optional status filter."""
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            row = self._connection.execute(
                f"SELECT COUNT(*) AS count FROM {WORKER_INSTANCES_TABLE} {where}",
                params,
            ).fetchone()
        except sqlite3.Error as exc:
            msg = "failed to count workers"
            raise RepositoryError(msg) from exc
        return int(row["count"]) if row is not None else 0

    def reconcile_stale_workers(
        self,
        *,
        now: datetime,
        stale_threshold_seconds: float,
        actor: str,
    ) -> StaleWorkerReconciliationResult:
        """Mark running/stopping workers with expired heartbeats as failed.

        Does not alter jobs. Expired job leases are recovered separately.
        """
        require_active_transaction(
            self._connection,
            operation="WorkerRepository.reconcile_stale_workers",
        )
        validate_identifier(actor, field_name="actor")
        if now.tzinfo is None:
            msg = "now must be timezone-aware"
            raise RepositoryError(msg)
        stale_threshold_seconds = validate_positive_finite_number(
            stale_threshold_seconds,
            field_name="stale_threshold_seconds",
        )
        cutoff = now - timedelta(seconds=stale_threshold_seconds)
        cutoff_text = format_utc_timestamp(cutoff)
        try:
            rows = self._connection.execute(
                f"""
                SELECT * FROM {WORKER_INSTANCES_TABLE}
                WHERE status IN ('running', 'stopping')
                  AND heartbeat_at <= ?
                ORDER BY heartbeat_at ASC, id ASC
                """,
                (cutoff_text,),
            ).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to select stale workers"
            raise RepositoryError(msg) from exc
        failed_ids: list[str] = []
        for row in rows:
            worker = self._worker_from_row(row)
            updated = self.mark_worker_failed(
                worker.id,
                expected_version=worker.version,
                stopped_at=now,
                error="worker heartbeat expired",
            )
            failed_ids.append(updated.id)
        return StaleWorkerReconciliationResult(
            scanned_count=len(rows),
            failed_count=len(failed_ids),
            failed_worker_ids=tuple(failed_ids),
        )

    def _transition_status(
        self,
        worker_id: str | uuid.UUID,
        *,
        expected_version: int,
        expected_statuses: tuple[WorkerStatus, ...],
        new_status: WorkerStatus,
        heartbeat_at: datetime,
        stopping_at: datetime | None,
        stopped_at: datetime | None,
        clear_current_job: bool,
        last_error: str | None,
        clear_last_error: bool,
    ) -> WorkerRecord:
        expected_version = validate_strict_int(
            expected_version,
            field_name="expected_version",
            minimum=1,
        )
        if heartbeat_at.tzinfo is None:
            msg = "heartbeat_at must be timezone-aware"
            raise RepositoryError(msg)
        normalized_id = normalize_uuid(worker_id)
        worker = self.get_worker(normalized_id)
        if worker is None:
            msg = f"worker not found: {normalized_id}"
            raise RepositoryError(msg)
        if worker.status in _TERMINAL_STATUSES:
            msg = f"cannot update terminal worker status {worker.status.value}"
            raise RepositoryError(msg)
        if worker.status not in expected_statuses:
            expected = ", ".join(status.value for status in expected_statuses)
            msg = (
                f"worker {normalized_id} expected status in ({expected}), "
                f"found {worker.status.value}"
            )
            raise RepositoryError(msg)
        if worker.version != expected_version:
            msg = (
                f"worker {normalized_id} expected version={expected_version}, "
                f"found version={worker.version}"
            )
            raise DatabaseIntegrityError(msg)

        new_version = worker.version + 1
        current_job_id = None if clear_current_job else worker.current_job_id
        if clear_last_error:
            error_value: str | None = None
        elif last_error is not None:
            error_value = last_error
        else:
            error_value = worker.last_error
        if stopping_at is not None:
            stopping_value: str | None = format_utc_timestamp(stopping_at)
        elif worker.stopping_at is not None:
            stopping_value = format_utc_timestamp(worker.stopping_at)
        else:
            stopping_value = None
        stopped_value = None if stopped_at is None else format_utc_timestamp(stopped_at)
        status_values = tuple(status.value for status in expected_statuses)
        placeholders = ", ".join("?" for _ in status_values)
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {WORKER_INSTANCES_TABLE}
                SET status = ?,
                    heartbeat_at = ?,
                    stopping_at = ?,
                    stopped_at = ?,
                    current_job_id = ?,
                    last_error = ?,
                    version = ?
                WHERE id = ? AND version = ? AND status IN ({placeholders})
                """,
                (
                    new_status.value,
                    format_utc_timestamp(heartbeat_at),
                    stopping_value,
                    stopped_value,
                    current_job_id,
                    error_value,
                    new_version,
                    normalized_id,
                    expected_version,
                    *status_values,
                ),
            )
            if cursor.rowcount != 1:
                msg = (
                    f"worker {normalized_id} status transition failed due to "
                    "concurrent modification"
                )
                raise DatabaseIntegrityError(msg)
        except DatabaseIntegrityError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error updating worker {normalized_id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to update worker {normalized_id}"
            raise RepositoryError(msg) from exc
        updated = self.get_worker(normalized_id)
        if updated is None:
            msg = f"worker {normalized_id} missing after status transition"
            raise RepositoryError(msg)
        return updated

    def _worker_from_row(self, row: sqlite3.Row) -> WorkerRecord:
        try:
            capabilities = loads_canonical_json(str(row["capabilities_json"]))
        except RepositoryError as exc:
            msg = f"malformed capabilities_json for worker {row['id']}"
            raise RepositoryError(msg) from exc
        return WorkerRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            status=WorkerStatus(str(row["status"])),
            process_id=int(row["process_id"]),
            hostname=str(row["hostname"]),
            started_at=parse_utc_timestamp(str(row["started_at"])),
            heartbeat_at=parse_utc_timestamp(str(row["heartbeat_at"])),
            stopping_at=(
                None if row["stopping_at"] is None else parse_utc_timestamp(str(row["stopping_at"]))
            ),
            stopped_at=(
                None if row["stopped_at"] is None else parse_utc_timestamp(str(row["stopped_at"]))
            ),
            current_job_id=(None if row["current_job_id"] is None else str(row["current_job_id"])),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            capabilities=capabilities,
            version=int(row["version"]),
        )

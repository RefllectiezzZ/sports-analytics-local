"""Queue-specific job claiming, lease, recovery, and finalization operations."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from sports_analytics.core.exceptions import (
    DatabaseIntegrityError,
    JobLeaseError,
    RepositoryError,
)
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
)
from sports_analytics.data.database import require_active_transaction
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.schema import JOB_EVENTS_TABLE, JOBS_TABLE, WORKER_INSTANCES_TABLE
from sports_analytics.data.types import (
    JobRecord,
    JobStatus,
    JsonValue,
    normalize_uuid,
    validate_identifier,
    validate_strict_int,
)
from sports_analytics.jobs.backoff import compute_retry_available_at
from sports_analytics.jobs.errors import sanitize_error_text
from sports_analytics.jobs.types import (
    JobClaim,
    JobExecutionOutcome,
    JobFinalizationKind,
    LeaseRecoveryResult,
    QueueStatus,
    WorkerRecord,
    WorkerStatus,
)


class JobQueueRepository:
    """Focused queue operations over jobs, job_events, and worker_instances.

    Does not own connection lifetime or transaction boundaries. Call claim and
    recovery methods inside ``transaction(connection, immediate=True)``.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._jobs = JobRepository(connection)
        self._workers = WorkerRepository(connection)

    def claim_next_job(
        self,
        *,
        worker_id: str | uuid.UUID,
        claimed_at: datetime,
        lease_duration_seconds: float,
        actor: str,
    ) -> JobClaim | None:
        """Atomically claim the next eligible pending job for a running worker.

        Selection order: priority ASC, available_at ASC, created_at ASC, id ASC.
        Must be called inside ``BEGIN IMMEDIATE``.
        """
        require_active_transaction(self._connection, operation="JobQueueRepository.claim_next_job")
        if claimed_at.tzinfo is None:
            msg = "claimed_at must be timezone-aware"
            raise RepositoryError(msg)
        if (
            isinstance(lease_duration_seconds, bool)
            or not isinstance(lease_duration_seconds, (int, float))
            or float(lease_duration_seconds) <= 0
        ):
            msg = "lease_duration_seconds must be a positive number"
            raise RepositoryError(msg)
        normalized_worker = normalize_uuid(worker_id)
        normalized_actor = validate_identifier(actor, field_name="actor")
        worker = self._workers.get_worker(normalized_worker)
        if worker is None:
            msg = f"cannot claim job: worker not found: {normalized_worker}"
            raise RepositoryError(msg)
        if worker.status is not WorkerStatus.RUNNING:
            msg = (
                f"cannot claim job: worker {normalized_worker} status is "
                f"{worker.status.value}, expected running"
            )
            raise RepositoryError(msg)

        claimed_text = format_utc_timestamp(claimed_at)
        lease_expires_at = claimed_at + timedelta(seconds=float(lease_duration_seconds))
        lease_text = format_utc_timestamp(lease_expires_at)
        try:
            row = self._connection.execute(
                f"""
                SELECT id, version FROM {JOBS_TABLE}
                WHERE status = 'pending'
                  AND available_at <= ?
                  AND attempts < maximum_attempts
                  AND lease_owner IS NULL
                  AND lease_expires_at IS NULL
                ORDER BY priority ASC, available_at ASC, created_at ASC, id ASC
                LIMIT 1
                """,
                (claimed_text,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = "failed to select next claimable job"
            raise RepositoryError(msg) from exc
        if row is None:
            return None

        job_id = str(row["id"])
        expected_version = int(row["version"])
        new_version = expected_version + 1
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET status = 'running',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    started_at = ?,
                    updated_at = ?,
                    finished_at = NULL,
                    result_json = NULL,
                    version = ?
                WHERE id = ?
                  AND status = 'pending'
                  AND version = ?
                  AND available_at <= ?
                  AND attempts < maximum_attempts
                  AND lease_owner IS NULL
                  AND lease_expires_at IS NULL
                """,
                (
                    normalized_worker,
                    lease_text,
                    claimed_text,
                    claimed_text,
                    new_version,
                    job_id,
                    expected_version,
                    claimed_text,
                ),
            )
            if cursor.rowcount != 1:
                msg = f"job {job_id} claim failed due to concurrent modification"
                raise DatabaseIntegrityError(msg)

            job = self._jobs.get_job(job_id)
            if job is None:
                msg = f"job {job_id} missing after claim"
                raise RepositoryError(msg)

            self._connection.execute(
                f"""
                INSERT INTO {JOB_EVENTS_TABLE} (
                    job_id, event_type, from_status, to_status, details_json,
                    occurred_at, actor, job_version
                ) VALUES (?, 'claimed', 'pending', 'running', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    dumps_canonical_json(
                        {
                            "worker_id": normalized_worker,
                            "attempt": job.attempts,
                            "maximum_attempts": job.maximum_attempts,
                            "lease_expires_at": lease_text,
                        }
                    ),
                    claimed_text,
                    normalized_actor,
                    new_version,
                ),
            )
            worker_cursor = self._connection.execute(
                f"""
                UPDATE {WORKER_INSTANCES_TABLE}
                SET current_job_id = ?,
                    heartbeat_at = ?,
                    version = version + 1
                WHERE id = ?
                  AND status = 'running'
                  AND version = ?
                """,
                (
                    job_id,
                    claimed_text,
                    normalized_worker,
                    worker.version,
                ),
            )
            if worker_cursor.rowcount != 1:
                msg = (
                    f"worker {normalized_worker} could not be updated during claim "
                    "due to concurrent modification"
                )
                raise DatabaseIntegrityError(msg)
        except (DatabaseIntegrityError, JobLeaseError):
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error claiming job {job_id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to claim job {job_id}"
            raise RepositoryError(msg) from exc

        return JobClaim(
            job=job,
            worker_id=normalized_worker,
            claimed_at=claimed_at,
            lease_expires_at=lease_expires_at,
            attempt=job.attempts,
        )

    def renew_job_lease(
        self,
        *,
        job_id: str | uuid.UUID,
        worker_id: str | uuid.UUID,
        heartbeat_at: datetime,
        lease_duration_seconds: float,
        expected_job_version: int | None = None,
    ) -> JobRecord:
        """Extend a running job lease without incrementing the job lifecycle version.

        Ordinary heartbeat renewals update ``updated_at`` and worker heartbeat but
        do not append job events and do not increment the job version.
        """
        require_active_transaction(self._connection, operation="JobQueueRepository.renew_job_lease")
        job, worker = self._require_active_lease(
            job_id=job_id,
            worker_id=worker_id,
            at=heartbeat_at,
            expected_job_version=expected_job_version,
            allow_stopping_worker=True,
        )
        if (
            isinstance(lease_duration_seconds, bool)
            or not isinstance(lease_duration_seconds, (int, float))
            or float(lease_duration_seconds) <= 0
        ):
            msg = "lease_duration_seconds must be a positive number"
            raise RepositoryError(msg)
        lease_expires_at = heartbeat_at + timedelta(seconds=float(lease_duration_seconds))
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                  AND lease_owner = ?
                  AND version = ?
                  AND lease_expires_at > ?
                """,
                (
                    format_utc_timestamp(lease_expires_at),
                    format_utc_timestamp(heartbeat_at),
                    job.id,
                    worker.id,
                    job.version,
                    format_utc_timestamp(heartbeat_at),
                ),
            )
            if cursor.rowcount != 1:
                msg = f"job {job.id} lease renewal failed due to concurrent modification"
                raise JobLeaseError(msg)
            worker_cursor = self._connection.execute(
                f"""
                UPDATE {WORKER_INSTANCES_TABLE}
                SET heartbeat_at = ?,
                    version = version + 1
                WHERE id = ?
                  AND status IN ('running', 'stopping')
                  AND current_job_id = ?
                  AND version = ?
                """,
                (
                    format_utc_timestamp(heartbeat_at),
                    worker.id,
                    job.id,
                    worker.version,
                ),
            )
            if worker_cursor.rowcount != 1:
                msg = f"worker {worker.id} heartbeat failed during lease renewal"
                raise JobLeaseError(msg)
        except JobLeaseError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error renewing lease for job {job.id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to renew lease for job {job.id}"
            raise RepositoryError(msg) from exc
        updated = self._jobs.get_job(job.id)
        if updated is None:
            msg = f"job {job.id} missing after lease renewal"
            raise RepositoryError(msg)
        return updated

    def complete_claimed_job(
        self,
        *,
        job_id: str | uuid.UUID,
        worker_id: str | uuid.UUID,
        expected_job_version: int,
        completed_at: datetime,
        result: JsonValue,
        actor: str,
    ) -> JobRecord:
        """Finalize a claimed job as succeeded under lease ownership checks."""
        require_active_transaction(
            self._connection,
            operation="JobQueueRepository.complete_claimed_job",
        )
        job, worker = self._require_active_lease(
            job_id=job_id,
            worker_id=worker_id,
            at=completed_at,
            expected_job_version=expected_job_version,
            allow_stopping_worker=True,
        )
        normalized_actor = validate_identifier(actor, field_name="actor")
        result_json = dumps_canonical_json(ensure_json_value(result))
        new_version = job.version + 1
        completed_text = format_utc_timestamp(completed_at)
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {JOBS_TABLE}
                SET status = 'succeeded',
                    finished_at = ?,
                    updated_at = ?,
                    result_json = ?,
                    last_error = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    version = ?
                WHERE id = ?
                  AND status = 'running'
                  AND lease_owner = ?
                  AND version = ?
                  AND lease_expires_at > ?
                """,
                (
                    completed_text,
                    completed_text,
                    result_json,
                    new_version,
                    job.id,
                    worker.id,
                    job.version,
                    completed_text,
                ),
            )
            if cursor.rowcount != 1:
                msg = f"job {job.id} success finalization failed due to concurrent modification"
                raise JobLeaseError(msg)
            self._connection.execute(
                f"""
                INSERT INTO {JOB_EVENTS_TABLE} (
                    job_id, event_type, from_status, to_status, details_json,
                    occurred_at, actor, job_version
                ) VALUES (?, 'succeeded', 'running', 'succeeded', ?, ?, ?, ?)
                """,
                (
                    job.id,
                    dumps_canonical_json({"worker_id": worker.id}),
                    completed_text,
                    normalized_actor,
                    new_version,
                ),
            )
            self._clear_worker_current_job(worker.id, job.id, completed_at, worker.version)
        except JobLeaseError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error completing job {job.id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to complete job {job.id}"
            raise RepositoryError(msg) from exc
        updated = self._jobs.get_job(job.id)
        if updated is None:
            msg = f"job {job.id} missing after success finalization"
            raise RepositoryError(msg)
        return updated

    def fail_claimed_job(
        self,
        *,
        job_id: str | uuid.UUID,
        worker_id: str | uuid.UUID,
        expected_job_version: int,
        failed_at: datetime,
        error: str,
        retryable: bool,
        actor: str,
        retry_backoff_base_seconds: float,
        retry_backoff_max_seconds: float,
        details: JsonValue | None = None,
    ) -> JobExecutionOutcome:
        """Finalize a claimed job as retry-scheduled or terminal failed."""
        require_active_transaction(
            self._connection,
            operation="JobQueueRepository.fail_claimed_job",
        )
        job, worker = self._require_active_lease(
            job_id=job_id,
            worker_id=worker_id,
            at=failed_at,
            expected_job_version=expected_job_version,
            allow_stopping_worker=True,
        )
        normalized_actor = validate_identifier(actor, field_name="actor")
        error_text = sanitize_error_text(error)
        event_details = ensure_json_value(details if details is not None else {})
        if not isinstance(event_details, dict):
            msg = "event details must be a JSON object"
            raise RepositoryError(msg)
        failed_text = format_utc_timestamp(failed_at)
        new_version = job.version + 1
        schedule_retry = bool(retryable) and job.attempts < job.maximum_attempts
        try:
            if schedule_retry:
                available_at = compute_retry_available_at(
                    failed_at=failed_at,
                    attempts=job.attempts,
                    base_seconds=retry_backoff_base_seconds,
                    max_seconds=retry_backoff_max_seconds,
                )
                available_text = format_utc_timestamp(available_at)
                cursor = self._connection.execute(
                    f"""
                    UPDATE {JOBS_TABLE}
                    SET status = 'pending',
                        available_at = ?,
                        updated_at = ?,
                        finished_at = NULL,
                        result_json = NULL,
                        last_error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        version = ?
                    WHERE id = ?
                      AND status = 'running'
                      AND lease_owner = ?
                      AND version = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        available_text,
                        failed_text,
                        error_text,
                        new_version,
                        job.id,
                        worker.id,
                        job.version,
                        failed_text,
                    ),
                )
                if cursor.rowcount != 1:
                    msg = f"job {job.id} retry finalization failed due to concurrent modification"
                    raise JobLeaseError(msg)
                detail_payload: dict[str, JsonValue] = {
                    "worker_id": worker.id,
                    "attempt": job.attempts,
                    "maximum_attempts": job.maximum_attempts,
                    "available_at": available_text,
                    "error": error_text,
                }
                detail_payload.update(event_details)
                self._connection.execute(
                    f"""
                    INSERT INTO {JOB_EVENTS_TABLE} (
                        job_id, event_type, from_status, to_status, details_json,
                        occurred_at, actor, job_version
                    ) VALUES (?, 'retry_scheduled', 'running', 'pending', ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        dumps_canonical_json(detail_payload),
                        failed_text,
                        normalized_actor,
                        new_version,
                    ),
                )
                kind = JobFinalizationKind.RETRY_SCHEDULED
            else:
                cursor = self._connection.execute(
                    f"""
                    UPDATE {JOBS_TABLE}
                    SET status = 'failed',
                        finished_at = ?,
                        updated_at = ?,
                        result_json = NULL,
                        last_error = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        version = ?
                    WHERE id = ?
                      AND status = 'running'
                      AND lease_owner = ?
                      AND version = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        failed_text,
                        failed_text,
                        error_text,
                        new_version,
                        job.id,
                        worker.id,
                        job.version,
                        failed_text,
                    ),
                )
                if cursor.rowcount != 1:
                    msg = f"job {job.id} failure finalization failed due to concurrent modification"
                    raise JobLeaseError(msg)
                detail_payload = {
                    "worker_id": worker.id,
                    "attempt": job.attempts,
                    "maximum_attempts": job.maximum_attempts,
                    "error": error_text,
                }
                detail_payload.update(event_details)
                self._connection.execute(
                    f"""
                    INSERT INTO {JOB_EVENTS_TABLE} (
                        job_id, event_type, from_status, to_status, details_json,
                        occurred_at, actor, job_version
                    ) VALUES (?, 'failed', 'running', 'failed', ?, ?, ?, ?)
                    """,
                    (
                        job.id,
                        dumps_canonical_json(detail_payload),
                        failed_text,
                        normalized_actor,
                        new_version,
                    ),
                )
                kind = JobFinalizationKind.FAILED
            self._clear_worker_current_job(worker.id, job.id, failed_at, worker.version)
        except JobLeaseError:
            raise
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error failing job {job.id}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to finalize failure for job {job.id}"
            raise RepositoryError(msg) from exc
        updated = self._jobs.get_job(job.id)
        if updated is None:
            msg = f"job {job.id} missing after failure finalization"
            raise RepositoryError(msg)
        return JobExecutionOutcome(kind=kind, job=updated)

    def recover_expired_leases(
        self,
        *,
        recovered_at: datetime,
        actor: str,
        retry_backoff_base_seconds: float,
        retry_backoff_max_seconds: float,
        maximum_rows: int,
    ) -> LeaseRecoveryResult:
        """Requeue or fail running jobs whose leases have expired.

        Must be called inside ``BEGIN IMMEDIATE``. Ordering is lease_expires_at ASC,
        updated_at ASC, id ASC.
        """
        require_active_transaction(
            self._connection,
            operation="JobQueueRepository.recover_expired_leases",
        )
        if recovered_at.tzinfo is None:
            msg = "recovered_at must be timezone-aware"
            raise RepositoryError(msg)
        maximum_rows = validate_strict_int(maximum_rows, field_name="maximum_rows", minimum=1)
        normalized_actor = validate_identifier(actor, field_name="actor")
        recovered_text = format_utc_timestamp(recovered_at)
        try:
            rows = self._connection.execute(
                f"""
                SELECT * FROM {JOBS_TABLE}
                WHERE status = 'running'
                  AND lease_owner IS NOT NULL
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at ASC, updated_at ASC, id ASC
                LIMIT ?
                """,
                (recovered_text, maximum_rows),
            ).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to select expired leases"
            raise RepositoryError(msg) from exc

        requeued_ids: list[str] = []
        failed_ids: list[str] = []
        for row in rows:
            job = self._jobs._job_from_row(row)  # noqa: SLF001 - shared mapper
            new_version = job.version + 1
            old_owner = job.lease_owner
            if job.attempts < job.maximum_attempts:
                available_at = compute_retry_available_at(
                    failed_at=recovered_at,
                    attempts=job.attempts,
                    base_seconds=retry_backoff_base_seconds,
                    max_seconds=retry_backoff_max_seconds,
                )
                error_text = sanitize_error_text("lease expired; requeued")
                try:
                    cursor = self._connection.execute(
                        f"""
                        UPDATE {JOBS_TABLE}
                        SET status = 'pending',
                            available_at = ?,
                            updated_at = ?,
                            finished_at = NULL,
                            result_json = NULL,
                            last_error = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            version = ?
                        WHERE id = ?
                          AND status = 'running'
                          AND version = ?
                          AND lease_expires_at <= ?
                        """,
                        (
                            format_utc_timestamp(available_at),
                            recovered_text,
                            error_text,
                            new_version,
                            job.id,
                            job.version,
                            recovered_text,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    self._connection.execute(
                        f"""
                        INSERT INTO {JOB_EVENTS_TABLE} (
                            job_id, event_type, from_status, to_status, details_json,
                            occurred_at, actor, job_version
                        ) VALUES (?, 'lease_expired_requeued', 'running', 'pending', ?, ?, ?, ?)
                        """,
                        (
                            job.id,
                            dumps_canonical_json(
                                {
                                    "previous_lease_owner": old_owner,
                                    "attempt": job.attempts,
                                    "available_at": format_utc_timestamp(available_at),
                                }
                            ),
                            recovered_text,
                            normalized_actor,
                            new_version,
                        ),
                    )
                except sqlite3.Error as exc:
                    msg = f"failed to requeue expired lease for job {job.id}"
                    raise RepositoryError(msg) from exc
                requeued_ids.append(job.id)
            else:
                error_text = sanitize_error_text("lease expired; maximum attempts exhausted")
                try:
                    cursor = self._connection.execute(
                        f"""
                        UPDATE {JOBS_TABLE}
                        SET status = 'failed',
                            finished_at = ?,
                            updated_at = ?,
                            result_json = NULL,
                            last_error = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            version = ?
                        WHERE id = ?
                          AND status = 'running'
                          AND version = ?
                          AND lease_expires_at <= ?
                        """,
                        (
                            recovered_text,
                            recovered_text,
                            error_text,
                            new_version,
                            job.id,
                            job.version,
                            recovered_text,
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    self._connection.execute(
                        f"""
                        INSERT INTO {JOB_EVENTS_TABLE} (
                            job_id, event_type, from_status, to_status, details_json,
                            occurred_at, actor, job_version
                        ) VALUES (?, 'lease_expired_failed', 'running', 'failed', ?, ?, ?, ?)
                        """,
                        (
                            job.id,
                            dumps_canonical_json(
                                {
                                    "previous_lease_owner": old_owner,
                                    "attempt": job.attempts,
                                }
                            ),
                            recovered_text,
                            normalized_actor,
                            new_version,
                        ),
                    )
                except sqlite3.Error as exc:
                    msg = f"failed to fail expired lease for job {job.id}"
                    raise RepositoryError(msg) from exc
                failed_ids.append(job.id)

            if old_owner is not None:
                try:
                    self._connection.execute(
                        f"""
                        UPDATE {WORKER_INSTANCES_TABLE}
                        SET current_job_id = NULL,
                            version = version + 1
                        WHERE id = ?
                          AND current_job_id = ?
                        """,
                        (old_owner, job.id),
                    )
                except sqlite3.Error as exc:
                    msg = f"failed to clear worker current_job_id for {old_owner}"
                    raise RepositoryError(msg) from exc

        return LeaseRecoveryResult(
            scanned_count=len(rows),
            requeued_count=len(requeued_ids),
            failed_count=len(failed_ids),
            requeued_job_ids=tuple(requeued_ids),
            failed_job_ids=tuple(failed_ids),
        )

    def cancel_pending_job(
        self,
        *,
        job_id: str | uuid.UUID,
        expected_status: JobStatus,
        expected_version: int,
        cancelled_at: datetime,
        actor: str,
        details: JsonValue | None = None,
    ) -> JobRecord:
        """Administratively cancel a pending or failed job.

        Running jobs cannot be force-cancelled in this release.
        """
        require_active_transaction(
            self._connection,
            operation="JobQueueRepository.cancel_pending_job",
        )
        if expected_status is JobStatus.RUNNING:
            msg = (
                "running jobs cannot be force-cancelled; cooperative cancellation "
                "is not implemented yet"
            )
            raise RepositoryError(msg)
        if expected_status not in {JobStatus.PENDING, JobStatus.FAILED}:
            msg = (
                f"cannot cancel job in status {expected_status.value}; "
                "only pending and failed jobs may be cancelled"
            )
            raise RepositoryError(msg)
        return self._jobs.transition_job(
            job_id,
            expected_status=expected_status,
            expected_version=expected_version,
            new_status=JobStatus.CANCELLED,
            actor=actor,
            occurred_at=cancelled_at,
            details=details,
        )

    def get_queue_status(
        self,
        *,
        now: datetime,
        stale_worker_threshold_seconds: float,
    ) -> QueueStatus:
        """Return a read-only queue and worker summary."""
        if now.tzinfo is None:
            msg = "now must be timezone-aware"
            raise RepositoryError(msg)
        if (
            isinstance(stale_worker_threshold_seconds, bool)
            or not isinstance(stale_worker_threshold_seconds, (int, float))
            or float(stale_worker_threshold_seconds) <= 0
        ):
            msg = "stale_worker_threshold_seconds must be a positive number"
            raise RepositoryError(msg)
        now_text = format_utc_timestamp(now)
        cutoff = now - timedelta(seconds=float(stale_worker_threshold_seconds))
        cutoff_text = format_utc_timestamp(cutoff)

        def _count(sql: str, params: tuple[object, ...] = ()) -> int:
            try:
                row = self._connection.execute(sql, params).fetchone()
            except sqlite3.Error as exc:
                msg = "failed to compute queue status"
                raise RepositoryError(msg) from exc
            return int(row["count"]) if row is not None else 0

        pending = _count(f"SELECT COUNT(*) AS count FROM {JOBS_TABLE} WHERE status = 'pending'")
        available = _count(
            f"""
            SELECT COUNT(*) AS count FROM {JOBS_TABLE}
            WHERE status = 'pending' AND available_at <= ?
            """,
            (now_text,),
        )
        delayed = _count(
            f"""
            SELECT COUNT(*) AS count FROM {JOBS_TABLE}
            WHERE status = 'pending' AND available_at > ?
            """,
            (now_text,),
        )
        running = _count(f"SELECT COUNT(*) AS count FROM {JOBS_TABLE} WHERE status = 'running'")
        succeeded = _count(f"SELECT COUNT(*) AS count FROM {JOBS_TABLE} WHERE status = 'succeeded'")
        failed = _count(f"SELECT COUNT(*) AS count FROM {JOBS_TABLE} WHERE status = 'failed'")
        cancelled = _count(f"SELECT COUNT(*) AS count FROM {JOBS_TABLE} WHERE status = 'cancelled'")
        expired = _count(
            f"""
            SELECT COUNT(*) AS count FROM {JOBS_TABLE}
            WHERE status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (now_text,),
        )
        active_workers = _count(
            f"""
            SELECT COUNT(*) AS count FROM {WORKER_INSTANCES_TABLE}
            WHERE status IN ('starting', 'running', 'stopping')
            """
        )
        stale_workers = _count(
            f"""
            SELECT COUNT(*) AS count FROM {WORKER_INSTANCES_TABLE}
            WHERE status IN ('running', 'stopping')
              AND heartbeat_at <= ?
            """,
            (cutoff_text,),
        )
        return QueueStatus(
            pending_count=pending,
            available_pending_count=available,
            delayed_pending_count=delayed,
            running_count=running,
            succeeded_count=succeeded,
            failed_count=failed,
            cancelled_count=cancelled,
            expired_running_lease_count=expired,
            active_worker_count=active_workers,
            stale_worker_count=stale_workers,
            observed_at=now,
        )

    def _require_active_lease(
        self,
        *,
        job_id: str | uuid.UUID,
        worker_id: str | uuid.UUID,
        at: datetime,
        expected_job_version: int | None,
        allow_stopping_worker: bool,
    ) -> tuple[JobRecord, WorkerRecord]:
        if at.tzinfo is None:
            msg = "timestamp must be timezone-aware"
            raise RepositoryError(msg)
        normalized_job = normalize_uuid(job_id)
        normalized_worker = normalize_uuid(worker_id)
        if expected_job_version is not None:
            expected_job_version = validate_strict_int(
                expected_job_version,
                field_name="expected_job_version",
                minimum=1,
            )
        job = self._jobs.get_job(normalized_job)
        if job is None:
            msg = f"job not found: {normalized_job}"
            raise RepositoryError(msg)
        if job.status is not JobStatus.RUNNING:
            msg = f"job {normalized_job} is not running"
            raise JobLeaseError(msg)
        if job.lease_owner != normalized_worker:
            msg = (
                f"job {normalized_job} lease_owner {job.lease_owner!r} "
                f"does not match worker {normalized_worker}"
            )
            raise JobLeaseError(msg)
        if job.lease_expires_at is None or job.lease_expires_at <= at:
            msg = f"job {normalized_job} lease is expired or missing"
            raise JobLeaseError(msg)
        if expected_job_version is not None and job.version != expected_job_version:
            msg = (
                f"job {normalized_job} expected version={expected_job_version}, "
                f"found version={job.version}"
            )
            raise JobLeaseError(msg)
        worker = self._workers.get_worker(normalized_worker)
        if worker is None:
            msg = f"worker not found: {normalized_worker}"
            raise JobLeaseError(msg)
        allowed = (
            {WorkerStatus.RUNNING, WorkerStatus.STOPPING}
            if allow_stopping_worker
            else {WorkerStatus.RUNNING}
        )
        if worker.status not in allowed:
            msg = f"worker {normalized_worker} status {worker.status.value} cannot own a lease"
            raise JobLeaseError(msg)
        if worker.current_job_id != normalized_job:
            msg = (
                f"worker {normalized_worker} current_job_id {worker.current_job_id!r} "
                f"does not match job {normalized_job}"
            )
            raise JobLeaseError(msg)
        return job, worker

    def _clear_worker_current_job(
        self,
        worker_id: str,
        job_id: str,
        at: datetime,
        expected_worker_version: int,
    ) -> None:
        cursor = self._connection.execute(
            f"""
            UPDATE {WORKER_INSTANCES_TABLE}
            SET current_job_id = NULL,
                heartbeat_at = ?,
                version = version + 1
            WHERE id = ?
              AND current_job_id = ?
              AND version = ?
              AND status IN ('running', 'stopping')
            """,
            (
                format_utc_timestamp(at),
                worker_id,
                job_id,
                expected_worker_version,
            ),
        )
        if cursor.rowcount != 1:
            msg = f"worker {worker_id} current_job_id could not be cleared for job {job_id}"
            raise JobLeaseError(msg)

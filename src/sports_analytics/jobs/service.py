"""Worker service layer owning SQLite connection and transaction boundaries."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sports_analytics.core.exceptions import RepositoryError
from sports_analytics.core.settings import WorkerSettings
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.workers import WorkerRepository
from sports_analytics.data.types import JsonValue
from sports_analytics.jobs.types import (
    DEFAULT_WORKER_NAME,
    JobClaim,
    JobExecutionOutcome,
    LeaseRecoveryResult,
    QueueStatus,
    StaleWorkerReconciliationResult,
    WorkerRecord,
    WorkerStatus,
)


class WorkerService:
    """Short-lived connection facade over worker and queue repositories."""

    def __init__(self, database_path: Path | str, settings: WorkerSettings) -> None:
        self._database_path = Path(database_path)
        self._settings = settings

    @property
    def database_path(self) -> Path:
        """Return the configured SQLite database path."""
        return self._database_path

    @property
    def settings(self) -> WorkerSettings:
        """Return immutable worker timing settings."""
        return self._settings

    def register_worker_starting(
        self,
        *,
        worker_id: str | None,
        process_id: int,
        hostname: str,
        started_at: datetime,
        capabilities: JsonValue,
        name: str = DEFAULT_WORKER_NAME,
    ) -> WorkerRecord:
        """Register a worker instance in ``starting`` status."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                return WorkerRepository(connection).register_worker(
                    worker_id=worker_id,
                    name=name,
                    process_id=process_id,
                    hostname=hostname,
                    started_at=started_at,
                    capabilities=capabilities,
                    status=WorkerStatus.STARTING,
                )

    def mark_running(self, *, worker_id: str, heartbeat_at: datetime) -> WorkerRecord:
        """Transition a registered worker to ``running``."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repository = WorkerRepository(connection)
                worker = self._require_worker(repository, worker_id)
                if worker.status is WorkerStatus.RUNNING:
                    return worker
                return repository.mark_worker_running(
                    worker.id,
                    expected_version=worker.version,
                    heartbeat_at=heartbeat_at,
                )

    def heartbeat_idle(self, *, worker_id: str, heartbeat_at: datetime) -> WorkerRecord:
        """Record an idle worker heartbeat without changing its current job."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repository = WorkerRepository(connection)
                worker = self._require_worker(repository, worker_id)
                return repository.heartbeat_worker(
                    worker.id,
                    expected_version=worker.version,
                    heartbeat_at=heartbeat_at,
                )

    def claim_next(
        self,
        *,
        worker_id: str,
        claimed_at: datetime,
        actor: str,
    ) -> JobClaim | None:
        """Atomically claim the next available pending job, if any."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                return JobQueueRepository(connection).claim_next_job(
                    worker_id=worker_id,
                    claimed_at=claimed_at,
                    lease_duration_seconds=self._settings.stale_job_timeout_seconds,
                    actor=actor,
                )

    def renew_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        heartbeat_at: datetime,
        expected_job_version: int,
    ) -> None:
        """Renew a running job lease and worker heartbeat."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                JobQueueRepository(connection).renew_job_lease(
                    job_id=job_id,
                    worker_id=worker_id,
                    heartbeat_at=heartbeat_at,
                    lease_duration_seconds=self._settings.stale_job_timeout_seconds,
                    expected_job_version=expected_job_version,
                )

    def complete(
        self,
        *,
        job_id: str,
        worker_id: str,
        expected_job_version: int,
        completed_at: datetime,
        result: JsonValue,
        actor: str,
    ) -> None:
        """Finalize a claimed job as succeeded."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                JobQueueRepository(connection).complete_claimed_job(
                    job_id=job_id,
                    worker_id=worker_id,
                    expected_job_version=expected_job_version,
                    completed_at=completed_at,
                    result=result,
                    actor=actor,
                )

    def fail(
        self,
        *,
        job_id: str,
        worker_id: str,
        expected_job_version: int,
        failed_at: datetime,
        error: str,
        retryable: bool,
        actor: str,
    ) -> JobExecutionOutcome:
        """Finalize a claimed job as retry-scheduled or terminal failed."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                return JobQueueRepository(connection).fail_claimed_job(
                    job_id=job_id,
                    worker_id=worker_id,
                    expected_job_version=expected_job_version,
                    failed_at=failed_at,
                    error=error,
                    retryable=retryable,
                    actor=actor,
                    retry_backoff_base_seconds=self._settings.retry_backoff_base_seconds,
                    retry_backoff_max_seconds=self._settings.retry_backoff_max_seconds,
                )

    def recover_expired(self, *, recovered_at: datetime, actor: str) -> LeaseRecoveryResult:
        """Recover one batch of expired running-job leases."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                return JobQueueRepository(connection).recover_expired_leases(
                    recovered_at=recovered_at,
                    actor=actor,
                    retry_backoff_base_seconds=self._settings.retry_backoff_base_seconds,
                    retry_backoff_max_seconds=self._settings.retry_backoff_max_seconds,
                    maximum_rows=self._settings.recovery_batch_size,
                )

    def reconcile_stale(
        self,
        *,
        reconciled_at: datetime,
        actor: str,
    ) -> StaleWorkerReconciliationResult:
        """Mark stale non-terminal workers as failed."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                return WorkerRepository(connection).reconcile_stale_workers(
                    now=reconciled_at,
                    stale_threshold_seconds=self._settings.stale_job_timeout_seconds,
                    actor=actor,
                )

    def get_queue_status(self, *, observed_at: datetime) -> QueueStatus:
        """Return a read-only queue and worker status snapshot."""
        with connect_database(self._database_path, read_only=True) as connection:
            return JobQueueRepository(connection).get_queue_status(
                now=observed_at,
                stale_worker_threshold_seconds=self._settings.stale_job_timeout_seconds,
            )

    def mark_stopping(self, *, worker_id: str, stopping_at: datetime) -> WorkerRecord:
        """Transition a running worker to ``stopping`` when possible."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repository = WorkerRepository(connection)
                worker = self._require_worker(repository, worker_id)
                if worker.status is WorkerStatus.STOPPING:
                    return worker
                if worker.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED}:
                    return worker
                return repository.mark_worker_stopping(
                    worker.id,
                    expected_version=worker.version,
                    stopping_at=stopping_at,
                )

    def mark_stopped(
        self,
        *,
        worker_id: str,
        stopped_at: datetime,
        shutdown_note: str | None = None,
    ) -> WorkerRecord:
        """Transition a non-terminal worker to ``stopped`` when possible."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repository = WorkerRepository(connection)
                worker = self._require_worker(repository, worker_id)
                if worker.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED}:
                    return worker
                return repository.mark_worker_stopped(
                    worker.id,
                    expected_version=worker.version,
                    stopped_at=stopped_at,
                    shutdown_note=shutdown_note,
                )

    def mark_failed(self, *, worker_id: str, failed_at: datetime, error: str) -> WorkerRecord:
        """Transition a non-terminal worker to ``failed`` when possible."""
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repository = WorkerRepository(connection)
                worker = self._require_worker(repository, worker_id)
                if worker.status in {WorkerStatus.STOPPED, WorkerStatus.FAILED}:
                    return worker
                return repository.mark_worker_failed(
                    worker.id,
                    expected_version=worker.version,
                    stopped_at=failed_at,
                    error=error,
                )

    @staticmethod
    def _require_worker(repository: WorkerRepository, worker_id: str) -> WorkerRecord:
        worker = repository.get_worker(worker_id)
        if worker is None:
            msg = f"worker not found: {worker_id}"
            raise RepositoryError(msg)
        return worker


JobQueueService = WorkerService
WorkerRegistrationService = WorkerService

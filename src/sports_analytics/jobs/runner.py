"""Sequential local worker runner with cooperative lease heartbeats."""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import FrameType

from sports_analytics.core.exceptions import (
    DatabaseError,
    JobLeaseError,
    JobRegistryError,
    PermanentJobError,
    RetryableJobError,
    RuntimeBootstrapError,
    SportsAnalyticsError,
    WorkerError,
    WorkerShutdownError,
)
from sports_analytics.core.runtime import RuntimeContext
from sports_analytics.data.codec import ensure_json_value
from sports_analytics.data.types import JsonValue, validate_positive_duration_seconds
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.jobs.errors import sanitize_error_text
from sports_analytics.jobs.registry import HandlerRegistry, build_default_registry
from sports_analytics.jobs.service import WorkerService
from sports_analytics.jobs.types import (
    JobClaim,
    JobExecutionState,
    JobFinalizationKind,
    WorkerRunResult,
    WorkerStatus,
)

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
Monotonic = Callable[[], float]
UuidFactory = Callable[[], str | uuid.UUID]
SignalHandler = signal.Handlers | int | Callable[[int, FrameType | None], object] | None


def utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(tz=UTC)


class LeaseHeartbeatController:
    """Renew one claimed job lease from a daemon thread."""

    def __init__(
        self,
        *,
        service: WorkerService,
        context: JobExecutionContext,
        expected_job_version: int,
        interval_seconds: float,
        clock: Clock,
        should_stop: Callable[[], bool],
    ) -> None:
        self._service = service
        self._context = context
        self._expected_job_version = expected_job_version
        self._interval_seconds = validate_positive_duration_seconds(
            interval_seconds,
            field_name="interval_seconds",
        )
        self._clock = clock
        self._should_stop = should_stop
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"job-lease-heartbeat-{context.job_id}",
            daemon=True,
        )

    def start(self) -> None:
        """Start the daemon heartbeat thread."""
        self._thread.start()

    def stop(self, *, timeout_seconds: float) -> bool:
        """Stop the heartbeat thread and return whether cleanup completed."""
        timeout = validate_positive_duration_seconds(
            timeout_seconds,
            field_name="timeout_seconds",
        )
        self._stop_requested.set()
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop_requested.wait(self._interval_seconds):
            if self._should_stop():
                self._context.request_stop()
            # Shutdown requests are cooperative for the handler via checkpoint().
            # Lease renewal continues until controller stop, lease loss, or failure.
            if self._context.is_lease_lost():
                return
            try:
                self._service.renew_lease(
                    job_id=self._context.job_id,
                    worker_id=self._context.worker_id,
                    heartbeat_at=self._clock(),
                    expected_job_version=self._expected_job_version,
                )
            except JobLeaseError as exc:
                self._context.report_lease_lost()
                self._context.logger.warning(
                    "job lease lost job_id=%s worker_id=%s error=%s",
                    self._context.job_id,
                    self._context.worker_id,
                    sanitize_error_text(exc),
                )
                return
            except Exception as exc:  # noqa: BLE001 - heartbeat failure invalidates fencing
                self._context.report_lease_lost()
                self._context.logger.error(
                    "job lease heartbeat failed job_id=%s worker_id=%s error=%s",
                    self._context.job_id,
                    self._context.worker_id,
                    sanitize_error_text(exc),
                )
                return


class LocalWorker:
    """Sequential durable-job worker for local execution."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        sleeper: Sleeper = time.sleep,
        monotonic: Monotonic = time.monotonic,
        pid: int | None = None,
        hostname: str | None = None,
        uuid_factory: UuidFactory = uuid.uuid4,
        install_signals: bool = True,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._pid = os.getpid() if pid is None else self._validate_pid(pid)
        self._hostname = (
            socket.gethostname() if hostname is None else self._validate_hostname(hostname)
        )
        self._uuid_factory = uuid_factory
        self._install_signals = install_signals
        self._active_context_lock = threading.Lock()
        self._active_context: JobExecutionContext | None = None

    def run(
        self,
        runtime_context: RuntimeContext,
        *,
        registry: HandlerRegistry | None = None,
        worker_id: str | None = None,
        once: bool = False,
        max_jobs: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> WorkerRunResult:
        """Run the local worker loop until stopped or a finite mode completes."""
        if type(once) is not bool:
            msg = "once must be a bool"
            raise WorkerError(msg)
        if max_jobs is not None and (type(max_jobs) is not int or max_jobs < 1):
            msg = "max_jobs must be a positive int when provided"
            raise WorkerError(msg)
        if once and max_jobs is not None:
            msg = "once and max_jobs cannot be combined"
            raise WorkerError(msg)

        selected_registry = registry if registry is not None else build_default_registry()
        selected_registry.freeze()
        service = WorkerService(runtime_context.database_path, runtime_context.settings.worker)
        local_stop = threading.Event()
        originals = self._install_signal_handlers(runtime_context, local_stop)
        durable_worker_id: str | None = None
        jobs_processed = 0
        stop_reason = "shutdown_requested"
        status = WorkerStatus.STOPPED
        try:
            generated_worker_id = str(self._uuid_factory()) if worker_id is None else worker_id
            worker = service.register_worker_starting(
                worker_id=generated_worker_id,
                process_id=self._pid,
                hostname=self._hostname,
                started_at=self._clock(),
                capabilities={"job_types": list(selected_registry.list_job_types())},
            )
            durable_worker_id = worker.id
            runtime_context.logger.info("worker registered worker_id=%s", durable_worker_id)
            worker = service.mark_running(worker_id=durable_worker_id, heartbeat_at=self._clock())
            status = worker.status
            self._run_startup_recovery(service, durable_worker_id, runtime_context)

            heartbeat_interval = runtime_context.settings.worker.heartbeat_interval_seconds
            stale_timeout = runtime_context.settings.worker.stale_job_timeout_seconds
            next_idle_heartbeat = self._monotonic() + heartbeat_interval
            next_recovery = self._monotonic() + stale_timeout
            while not self._is_stop_requested(local_stop, stop_event):
                now_monotonic = self._monotonic()
                if now_monotonic >= next_idle_heartbeat:
                    service.heartbeat_idle(worker_id=durable_worker_id, heartbeat_at=self._clock())
                    next_idle_heartbeat = self._monotonic() + heartbeat_interval

                if now_monotonic >= next_recovery:
                    service.recover_expired(recovered_at=self._clock(), actor=durable_worker_id)
                    next_recovery = self._monotonic() + stale_timeout

                claim = service.claim_next(
                    worker_id=durable_worker_id,
                    claimed_at=self._clock(),
                    actor=durable_worker_id,
                )
                if claim is None:
                    if once:
                        stop_reason = "once_no_job"
                        break
                    if self._sleep_until_stop(
                        runtime_context.settings.worker.poll_interval_seconds,
                        local_stop=local_stop,
                        stop_event=stop_event,
                    ):
                        stop_reason = "shutdown_requested"
                        break
                    continue

                state = self._execute_claim(
                    service=service,
                    registry=selected_registry,
                    claim=claim,
                    runtime_context=runtime_context,
                    local_stop=local_stop,
                    stop_event=stop_event,
                )
                jobs_processed += 1
                next_idle_heartbeat = self._monotonic() + heartbeat_interval
                if state is JobExecutionState.LEASE_LOST:
                    msg = (
                        f"worker {durable_worker_id} lost lease for job {claim.job.id}; "
                        "refusing further claims in this process"
                    )
                    raise JobLeaseError(msg)
                if state is JobExecutionState.SHUTDOWN_INTERRUPTED:
                    stop_reason = state.value
                    break
                if max_jobs is not None and jobs_processed >= max_jobs:
                    stop_reason = "max_jobs"
                    break
                if once:
                    stop_reason = "once"
                    break

            if durable_worker_id is not None:
                service.mark_stopping(worker_id=durable_worker_id, stopping_at=self._clock())
                worker = service.mark_stopped(worker_id=durable_worker_id, stopped_at=self._clock())
                status = worker.status
            return WorkerRunResult(
                worker_id=durable_worker_id or "",
                jobs_processed=jobs_processed,
                stop_reason=stop_reason,
                status=status,
            )
        except BaseException as exc:
            if durable_worker_id is not None:
                try:
                    worker = service.mark_failed(
                        worker_id=durable_worker_id,
                        failed_at=self._clock(),
                        error=sanitize_error_text(exc),
                    )
                    status = worker.status
                except Exception as mark_exc:  # noqa: BLE001 - preserve primary failure
                    runtime_context.logger.error(
                        "worker failure status update failed worker_id=%s error=%s",
                        durable_worker_id,
                        sanitize_error_text(mark_exc),
                    )
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            if isinstance(exc, SportsAnalyticsError | RuntimeBootstrapError | DatabaseError):
                raise
            msg = f"worker failed: {sanitize_error_text(exc)}"
            raise WorkerError(msg) from exc
        finally:
            self._restore_signal_handlers(originals)
            with self._active_context_lock:
                self._active_context = None

    def _run_startup_recovery(
        self,
        service: WorkerService,
        worker_id: str,
        runtime_context: RuntimeContext,
    ) -> None:
        stale = service.reconcile_stale(reconciled_at=self._clock(), actor=worker_id)
        recovered = service.recover_expired(recovered_at=self._clock(), actor=worker_id)
        runtime_context.logger.info(
            "worker startup recovery worker_id=%s stale_scanned=%s stale_failed=%s "
            "leases_scanned=%s leases_requeued=%s leases_failed=%s",
            worker_id,
            stale.scanned_count,
            stale.failed_count,
            recovered.scanned_count,
            recovered.requeued_count,
            recovered.failed_count,
        )

    def _execute_claim(
        self,
        *,
        service: WorkerService,
        registry: HandlerRegistry,
        claim: JobClaim,
        runtime_context: RuntimeContext,
        local_stop: threading.Event,
        stop_event: threading.Event | None,
    ) -> JobExecutionState:
        job = claim.job
        logger = runtime_context.logger
        context = JobExecutionContext(
            job_id=job.id,
            worker_id=claim.worker_id,
            attempt=claim.attempt,
            maximum_attempts=job.maximum_attempts,
            claimed_at=claim.claimed_at,
            lease_expires_at=claim.lease_expires_at,
            logger=logger,
        )
        context.bind_runtime(runtime_context)
        controller = LeaseHeartbeatController(
            service=service,
            context=context,
            expected_job_version=job.version,
            interval_seconds=runtime_context.settings.worker.heartbeat_interval_seconds,
            clock=self._clock,
            should_stop=lambda: self._is_stop_requested(local_stop, stop_event),
        )
        failure: tuple[str, bool, JobExecutionState | None] | None = None
        result: JsonValue | None = None
        early_state: JobExecutionState | None = None
        with self._active_context_lock:
            self._active_context = context
        controller.start()
        try:
            try:
                if self._is_stop_requested(local_stop, stop_event):
                    context.request_stop()
                context.checkpoint()
                handler = registry.get(job.job_type)
                result = ensure_json_value(handler(context, job.payload))
                context.checkpoint()
            except RetryableJobError as exc:
                failure = (sanitize_error_text(exc), True, None)
            except PermanentJobError as exc:
                failure = (sanitize_error_text(exc), False, None)
            except WorkerShutdownError:
                context.request_stop()
                # Leave the running lease in place; recovery requeues after expiry.
                early_state = JobExecutionState.SHUTDOWN_INTERRUPTED
            except JobLeaseError as exc:
                context.report_lease_lost()
                logger.warning(
                    "job lease lost before finalization job_id=%s worker_id=%s error=%s",
                    job.id,
                    claim.worker_id,
                    sanitize_error_text(exc),
                )
                early_state = JobExecutionState.LEASE_LOST
            except JobRegistryError as exc:
                failure = (sanitize_error_text(exc), False, None)
            except Exception as exc:  # noqa: BLE001 - unexpected handler errors are retryable
                failure = (sanitize_error_text(exc), True, None)
        except BaseException:
            raise
        finally:
            stopped = controller.stop(
                timeout_seconds=runtime_context.settings.worker.shutdown_grace_seconds
            )
            if not stopped:
                context.report_lease_lost()
                logger.error(
                    "job lease heartbeat cleanup timed out job_id=%s worker_id=%s",
                    job.id,
                    claim.worker_id,
                )
            with self._active_context_lock:
                self._active_context = None

        # Any locally observed lease loss after the final checkpoint prevents finalization.
        if context.is_lease_lost() or early_state is JobExecutionState.LEASE_LOST:
            return JobExecutionState.LEASE_LOST
        if early_state is JobExecutionState.SHUTDOWN_INTERRUPTED:
            return early_state

        if failure is not None:
            error_text, retryable, preferred_state = failure
            return self._finalize_failure(
                service=service,
                claim=claim,
                error_text=error_text,
                retryable=retryable,
                preferred_state=preferred_state,
            )

        service.complete(
            job_id=job.id,
            worker_id=claim.worker_id,
            expected_job_version=job.version,
            completed_at=self._clock(),
            result={} if result is None else result,
            actor=claim.worker_id,
        )
        logger.info(
            "job succeeded job_id=%s job_type=%s worker_id=%s attempt=%s",
            job.id,
            job.job_type,
            claim.worker_id,
            claim.attempt,
        )
        return JobExecutionState.SUCCEEDED

    def _finalize_failure(
        self,
        *,
        service: WorkerService,
        claim: JobClaim,
        error_text: str,
        retryable: bool,
        preferred_state: JobExecutionState | None,
    ) -> JobExecutionState:
        outcome = service.fail(
            job_id=claim.job.id,
            worker_id=claim.worker_id,
            expected_job_version=claim.job.version,
            failed_at=self._clock(),
            error=error_text,
            retryable=retryable,
            actor=claim.worker_id,
        )
        claim_state = self._state_from_finalization(outcome.kind)
        if preferred_state is not None:
            return preferred_state
        return claim_state

    @staticmethod
    def _state_from_finalization(kind: JobFinalizationKind) -> JobExecutionState:
        if kind is JobFinalizationKind.RETRY_SCHEDULED:
            return JobExecutionState.RETRY_SCHEDULED
        if kind is JobFinalizationKind.FAILED:
            return JobExecutionState.FAILED
        return JobExecutionState.SUCCEEDED

    def _sleep_until_stop(
        self,
        seconds: float,
        *,
        local_stop: threading.Event,
        stop_event: threading.Event | None,
    ) -> bool:
        deadline = self._monotonic() + max(0.0, float(seconds))
        while True:
            if self._is_stop_requested(local_stop, stop_event):
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleeper(min(remaining, 1.0))

    def _is_stop_requested(
        self,
        local_stop: threading.Event,
        stop_event: threading.Event | None,
    ) -> bool:
        if local_stop.is_set() or (stop_event is not None and stop_event.is_set()):
            with self._active_context_lock:
                if self._active_context is not None:
                    self._active_context.request_stop()
            return True
        return False

    def _request_stop(self, local_stop: threading.Event) -> None:
        """Propagate a cooperative stop outside the signal-handler path."""
        local_stop.set()
        with self._active_context_lock:
            if self._active_context is not None:
                self._active_context.request_stop()

    def _install_signal_handlers(
        self,
        runtime_context: RuntimeContext,
        local_stop: threading.Event,
    ) -> dict[int, SignalHandler]:
        del runtime_context
        if not self._install_signals or threading.current_thread() is not threading.main_thread():
            return {}

        originals: dict[int, SignalHandler] = {}

        def _handler(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            local_stop.set()

        for signum_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, signum_name, None)
            if signum is None:
                continue
            originals[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        return originals

    @staticmethod
    def _restore_signal_handlers(originals: dict[int, SignalHandler]) -> None:
        for signum, handler in originals.items():
            signal.signal(signum, handler)

    @staticmethod
    def _validate_pid(pid: int) -> int:
        if type(pid) is not int or pid <= 0:
            msg = "pid must be a positive int"
            raise WorkerError(msg)
        return pid

    @staticmethod
    def _validate_hostname(hostname: str) -> str:
        if not isinstance(hostname, str) or not hostname.strip():
            msg = "hostname must be a non-empty string"
            raise WorkerError(msg)
        return hostname.strip()

"""Command-line interface for the local durable-job worker."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, handle_common_modes
from sports_analytics.core.cli import build_argument_parser as build_common_argument_parser
from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    RuntimeBootstrapError,
    WorkerError,
)
from sports_analytics.core.runtime import bootstrap_runtime
from sports_analytics.jobs.runner import LocalWorker
from sports_analytics.jobs.service import WorkerService
from sports_analytics.jobs.types import LeaseRecoveryResult, QueueStatus, WorkerRunResult


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the worker argument parser."""
    parser = build_common_argument_parser("worker", "Background job worker.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one currently available job and then exit.",
    )
    parser.add_argument(
        "--max-jobs",
        type=_positive_int,
        default=None,
        metavar="N",
        help="Stop after processing N claimed jobs.",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        metavar="UUID",
        help="Explicit durable worker instance UUID.",
    )
    parser.add_argument(
        "--queue-status",
        action="store_true",
        help="Print queue and worker status, then exit.",
    )
    parser.add_argument(
        "--recover-expired-leases",
        action="store_true",
        help="Recover one batch of expired job leases, then exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the worker CLI."""
    parser = build_argument_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _validate_mutual_exclusion(parser, args)
    try:
        common_exit = handle_common_modes(args)
        if common_exit is not None:
            return common_exit

        runtime_context = bootstrap_runtime(
            "worker",
            config_path=args.config,
            env_file=args.env_file,
        )
        service = WorkerService(runtime_context.database_path, runtime_context.settings.worker)
        if args.queue_status:
            status = service.get_queue_status(observed_at=runtime_context.started_at)
            print(format_queue_status(status))
            return SUCCESS_EXIT
        if args.recover_expired_leases:
            recovery = service.recover_expired(
                recovered_at=runtime_context.started_at,
                actor="worker-cli",
            )
            print(format_lease_recovery(recovery))
            return SUCCESS_EXIT

        worker_result = LocalWorker().run(
            runtime_context,
            worker_id=args.worker_id,
            once=args.once,
            max_jobs=args.max_jobs,
        )
        print(format_worker_result(worker_result))
        return SUCCESS_EXIT
    except (ConfigurationError, RuntimeBootstrapError, DatabaseError, WorkerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def format_queue_status(status: QueueStatus) -> str:
    """Return the required queue status output line."""
    return (
        "queue status: "
        f"pending={status.pending_count} "
        f"available={status.available_pending_count} "
        f"delayed={status.delayed_pending_count} "
        f"running={status.running_count} "
        f"expired={status.expired_running_lease_count} "
        f"succeeded={status.succeeded_count} "
        f"failed={status.failed_count} "
        f"cancelled={status.cancelled_count} "
        f"workers_active={status.active_worker_count} "
        f"workers_stale={status.stale_worker_count}"
    )


def format_lease_recovery(result: LeaseRecoveryResult) -> str:
    """Return the required lease recovery output line."""
    return (
        "lease recovery: "
        f"scanned={result.scanned_count} "
        f"requeued={result.requeued_count} "
        f"failed={result.failed_count}"
    )


def format_worker_result(result: WorkerRunResult) -> str:
    """Return concise worker run completion output."""
    return (
        "worker stopped: "
        f"worker_id={result.worker_id} "
        f"jobs_processed={result.jobs_processed} "
        f"stop_reason={result.stop_reason} "
        f"status={result.status.value}"
    )


def _validate_mutual_exclusion(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    worker_modes = [args.queue_status, args.recover_expired_leases]
    if sum(1 for enabled in worker_modes if enabled) > 1:
        parser.error("--queue-status and --recover-expired-leases are mutually exclusive")
    common_mode = args.validate_config or args.database_status or args.migrate_database
    if common_mode and any(worker_modes):
        parser.error("worker one-shot modes cannot be combined with shared CLI modes")
    if any(worker_modes) and (args.once or args.max_jobs is not None or args.worker_id is not None):
        parser.error("queue status and recovery modes cannot be combined with worker run options")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed

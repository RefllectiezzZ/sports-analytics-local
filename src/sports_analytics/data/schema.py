"""Operational schema constants for the local SQLite database."""

from __future__ import annotations

from typing import Final

SCHEMA_MIGRATIONS_TABLE: Final[str] = "schema_migrations"
APPLICATION_METADATA_TABLE: Final[str] = "application_metadata"
JOBS_TABLE: Final[str] = "jobs"
JOB_EVENTS_TABLE: Final[str] = "job_events"
WORKER_INSTANCES_TABLE: Final[str] = "worker_instances"
SNAPSHOTS_TABLE: Final[str] = "snapshots"
AUDIT_EVENTS_TABLE: Final[str] = "audit_events"

OPERATIONAL_TABLES: Final[tuple[str, ...]] = (
    APPLICATION_METADATA_TABLE,
    JOBS_TABLE,
    JOB_EVENTS_TABLE,
    WORKER_INSTANCES_TABLE,
    SNAPSHOTS_TABLE,
    AUDIT_EVENTS_TABLE,
)

EXPECTED_INDEXES: Final[tuple[str, ...]] = (
    "idx_jobs_pending_order",
    "idx_jobs_status_type",
    "uq_jobs_idempotency_key",
    "idx_jobs_running_lease_expires",
    "idx_job_events_job_occurred",
    "idx_worker_instances_status_heartbeat",
    "idx_worker_instances_heartbeat",
    "uq_worker_instances_current_job",
    "idx_snapshots_type_status_created",
    "uq_snapshots_relative_path",
    "idx_audit_events_occurred",
    "idx_audit_events_entity",
    "idx_audit_events_correlation",
)

EXPECTED_TRIGGERS: Final[tuple[str, ...]] = (
    "trg_jobs_running_lease_insert",
    "trg_jobs_running_lease_update",
    "trg_worker_instances_current_job_insert",
    "trg_worker_instances_current_job_update",
)

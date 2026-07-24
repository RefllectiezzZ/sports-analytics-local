"""Operational schema constants for the local SQLite database."""

from __future__ import annotations

from typing import Final

SCHEMA_MIGRATIONS_TABLE: Final[str] = "schema_migrations"
APPLICATION_METADATA_TABLE: Final[str] = "application_metadata"
JOBS_TABLE: Final[str] = "jobs"
JOB_EVENTS_TABLE: Final[str] = "job_events"
SNAPSHOTS_TABLE: Final[str] = "snapshots"
AUDIT_EVENTS_TABLE: Final[str] = "audit_events"

OPERATIONAL_TABLES: Final[tuple[str, ...]] = (
    APPLICATION_METADATA_TABLE,
    JOBS_TABLE,
    JOB_EVENTS_TABLE,
    SNAPSHOTS_TABLE,
    AUDIT_EVENTS_TABLE,
)

EXPECTED_INDEXES: Final[tuple[str, ...]] = (
    "idx_jobs_pending_order",
    "idx_jobs_status_type",
    "uq_jobs_idempotency_key",
    "idx_job_events_job_occurred",
    "idx_snapshots_type_status_created",
    "uq_snapshots_relative_path",
    "idx_audit_events_occurred",
    "idx_audit_events_entity",
    "idx_audit_events_correlation",
)

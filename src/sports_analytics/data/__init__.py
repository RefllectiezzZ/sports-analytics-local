"""Data access, SQLite persistence, and dataset I/O."""

from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import (
    apply_migrations,
    discover_migrations,
    ensure_database_ready,
    get_migration_status,
)
from sports_analytics.data.service import initialize_operational_database
from sports_analytics.data.types import (
    AuditEventRecord,
    DatabaseReadiness,
    JobEventRecord,
    JobRecord,
    JobStatus,
    MigrationStatus,
    SnapshotRecord,
    SnapshotStatus,
)

__all__ = [
    "AuditEventRecord",
    "DatabaseReadiness",
    "JobEventRecord",
    "JobRecord",
    "JobStatus",
    "MigrationStatus",
    "SnapshotRecord",
    "SnapshotStatus",
    "apply_migrations",
    "connect_database",
    "discover_migrations",
    "ensure_database_ready",
    "get_migration_status",
    "initialize_operational_database",
    "transaction",
]

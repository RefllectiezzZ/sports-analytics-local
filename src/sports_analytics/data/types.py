"""Typed records, enums, and validation helpers for operational persistence."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from sports_analytics.core.exceptions import RepositoryError

JsonPrimitive = None | bool | int | float | str
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]

DEFAULT_JOB_PRIORITY: Final[int] = 100
MAX_IDENTIFIER_LENGTH: Final[int] = 128
MAX_METADATA_KEY_LENGTH: Final[int] = 256
MAX_TEXT_VALUE_LENGTH: Final[int] = 1_048_576
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_CHECKSUM_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class JobStatus(StrEnum):
    """Allowed durable job statuses."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SnapshotStatus(StrEnum):
    """Allowed snapshot metadata statuses."""

    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Immutable job row representation."""

    id: str
    job_type: str
    payload: JsonValue
    status: JobStatus
    priority: int
    attempts: int
    maximum_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    idempotency_key: str | None
    result: JsonValue | None
    version: int


@dataclass(frozen=True, slots=True)
class JobEventRecord:
    """Immutable append-only job event representation."""

    id: int
    job_id: str
    event_type: str
    from_status: JobStatus | None
    to_status: JobStatus | None
    details: JsonValue
    occurred_at: datetime
    actor: str
    job_version: int


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """Immutable snapshot metadata representation."""

    id: str
    snapshot_type: str
    status: SnapshotStatus
    relative_path: str
    checksum_sha256: str | None
    row_count: int | None
    source_name: str
    source_version: str | None
    schema_version: str
    created_at: datetime
    ready_at: datetime | None
    metadata: JsonValue
    version: int


@dataclass(frozen=True, slots=True)
class AuditEventRecord:
    """Immutable append-only audit event representation."""

    id: int
    event_type: str
    entity_type: str
    entity_id: str | None
    actor: str
    occurred_at: datetime
    correlation_id: str | None
    details: JsonValue


@dataclass(frozen=True, slots=True)
class Migration:
    """Packaged forward-only migration definition."""

    version: int
    name: str
    sql_text: str
    checksum: str
    filename: str


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """Recorded applied migration metadata."""

    version: int
    name: str
    checksum: str
    applied_at: datetime
    execution_time_ms: int


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """Immutable migration status summary."""

    database_path: Path
    current_version: int
    latest_version: int
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    checksums_valid: bool
    is_up_to_date: bool


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    """Result of ensuring the operational database is migrated and ready."""

    database_path: Path
    previous_version: int
    schema_version: int
    migrations_applied: tuple[Migration, ...]
    status: MigrationStatus


def normalize_uuid(value: str | uuid.UUID | None = None) -> str:
    """Return a lower-case canonical UUID string."""
    if value is None:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        msg = f"invalid UUID identifier: {value!r}"
        raise RepositoryError(msg) from exc


def validate_identifier(value: str, *, field_name: str) -> str:
    """Validate a stable lowercase identifier used by repositories."""
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise RepositoryError(msg)
    if value != value.strip():
        msg = f"{field_name} must not have leading or trailing whitespace"
        raise RepositoryError(msg)
    if not value:
        msg = f"{field_name} must be non-empty"
        raise RepositoryError(msg)
    if len(value) > MAX_IDENTIFIER_LENGTH:
        msg = f"{field_name} exceeds maximum length of {MAX_IDENTIFIER_LENGTH}"
        raise RepositoryError(msg)
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        msg = (
            f"{field_name} must be a lowercase identifier using letters, digits, "
            "underscore, hyphen, period, or colon"
        )
        raise RepositoryError(msg)
    return value


def validate_metadata_key(value: str) -> str:
    """Validate an application metadata key."""
    if not isinstance(value, str):
        msg = "metadata key must be a string"
        raise RepositoryError(msg)
    if value != value.strip():
        msg = "metadata key must not have leading or trailing whitespace"
        raise RepositoryError(msg)
    if not value:
        msg = "metadata key must be non-empty"
        raise RepositoryError(msg)
    if len(value) > MAX_METADATA_KEY_LENGTH:
        msg = f"metadata key exceeds maximum length of {MAX_METADATA_KEY_LENGTH}"
        raise RepositoryError(msg)
    return value


def validate_plain_text(value: str, *, field_name: str) -> str:
    """Validate a plain UTF-8 text value for storage."""
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise RepositoryError(msg)
    if len(value) > MAX_TEXT_VALUE_LENGTH:
        msg = f"{field_name} exceeds maximum length of {MAX_TEXT_VALUE_LENGTH}"
        raise RepositoryError(msg)
    return value


def validate_relative_snapshot_path(value: str) -> str:
    """Validate a relative POSIX-style snapshot path without traversal."""
    if not isinstance(value, str):
        msg = "relative_path must be a string"
        raise RepositoryError(msg)
    if value != value.strip():
        msg = "relative_path must not have leading or trailing whitespace"
        raise RepositoryError(msg)
    if not value:
        msg = "relative_path must be non-empty"
        raise RepositoryError(msg)
    if "\\" in value:
        msg = "relative_path must use POSIX-style separators"
        raise RepositoryError(msg)
    if value.startswith("/"):
        msg = "relative_path must be relative, not absolute"
        raise RepositoryError(msg)
    path = PurePosixPath(value)
    if path.is_absolute():
        msg = "relative_path must be relative, not absolute"
        raise RepositoryError(msg)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        msg = "relative_path must not contain empty segments, '.', or '..'"
        raise RepositoryError(msg)
    return path.as_posix()


def validate_sha256_checksum(value: str) -> str:
    """Validate a lowercase SHA-256 hex digest."""
    if not isinstance(value, str):
        msg = "checksum_sha256 must be a string"
        raise RepositoryError(msg)
    if not _CHECKSUM_HEX_PATTERN.fullmatch(value):
        msg = "checksum_sha256 must be exactly 64 lowercase hexadecimal characters"
        raise RepositoryError(msg)
    return value


def validate_limit_offset(*, limit: int | None, offset: int) -> tuple[int | None, int]:
    """Validate pagination arguments."""
    if offset < 0:
        msg = "offset must be >= 0"
        raise RepositoryError(msg)
    if limit is not None and limit < 0:
        msg = "limit must be >= 0"
        raise RepositoryError(msg)
    return limit, offset


def is_migration_checksum(value: str) -> bool:
    """Return whether ``value`` looks like a SHA-256 hex digest."""
    return bool(_SHA256_PATTERN.fullmatch(value))

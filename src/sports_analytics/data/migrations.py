"""Internal forward-only SQLite migration discovery and application."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Final

from sports_analytics.core.exceptions import (
    DatabaseConnectionError,
    DatabaseMigrationError,
)
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp, utc_now
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.schema import SCHEMA_MIGRATIONS_TABLE
from sports_analytics.data.types import (
    AppliedMigration,
    DatabaseReadiness,
    Migration,
    MigrationStatus,
)

_MIGRATION_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<version>\d{4})_(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\.sql$"
)
_PROHIBITED_STATEMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|VACUUM|ATTACH|DETACH)\b",
    re.IGNORECASE,
)
_PROHIBITED_PRAGMA_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|synchronous|foreign_keys|busy_timeout|locking_mode|"
    r"temp_store|query_only|writable_schema)\b",
    re.IGNORECASE,
)
_SCHEMA_MIGRATIONS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    execution_time_ms INTEGER NOT NULL
)
"""


def discover_migrations(
    *,
    package: str = "sports_analytics.data.sql.migrations",
) -> tuple[Migration, ...]:
    """Discover packaged migrations deterministically by numeric version."""
    try:
        root = resources.files(package)
    except (ModuleNotFoundError, TypeError) as exc:
        msg = f"unable to locate migration package {package!r}: {exc}"
        raise DatabaseMigrationError(msg) from exc

    discovered: list[Migration] = []
    seen_versions: set[int] = set()
    for entry in root.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if name == "__init__.py" or name.startswith("."):
            continue
        match = _MIGRATION_FILENAME_PATTERN.fullmatch(name)
        if match is None:
            msg = f"malformed migration filename rejected: {name!r}"
            raise DatabaseMigrationError(msg)
        version = int(match.group("version"))
        migration_name = match.group("name")
        if version in seen_versions:
            msg = f"duplicate migration version detected: {version}"
            raise DatabaseMigrationError(msg)
        seen_versions.add(version)
        sql_text = entry.read_text(encoding="utf-8")
        normalized = _normalize_migration_text(sql_text)
        checksum = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        validate_migration_sql(sql_text, filename=name)
        discovered.append(
            Migration(
                version=version,
                name=migration_name,
                sql_text=sql_text,
                checksum=checksum,
                filename=name,
            )
        )

    ordered = tuple(sorted(discovered, key=lambda item: item.version))
    if not ordered:
        return ordered

    expected = list(range(1, ordered[-1].version + 1))
    actual = [item.version for item in ordered]
    if actual != expected:
        msg = (
            "migration versions must be consecutive starting at 1; "
            f"found {actual}, expected {expected}"
        )
        raise DatabaseMigrationError(msg)
    return ordered


def validate_migration_sql(sql_text: str, *, filename: str) -> None:
    """Reject prohibited transaction-control or unsafe statements."""
    for statement in split_sql_statements(sql_text):
        if _PROHIBITED_STATEMENT_PATTERN.match(statement):
            msg = f"prohibited statement in migration {filename}: {statement.split()[0]}"
            raise DatabaseMigrationError(msg)
        if _PROHIBITED_PRAGMA_PATTERN.match(statement):
            msg = f"prohibited PRAGMA in migration {filename}"
            raise DatabaseMigrationError(msg)


def split_sql_statements(sql_text: str) -> list[str]:
    """Split migration SQL into executable statements without using executescript."""
    without_line_comments = _strip_line_comments(sql_text)
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    index = 0
    length = len(without_line_comments)
    while index < length:
        char = without_line_comments[index]
        if char == "'" and not in_single:
            in_single = True
            current.append(char)
            index += 1
            continue
        if char == "'" and in_single:
            # Handle escaped single quotes ('') inside SQL string literals.
            if index + 1 < length and without_line_comments[index + 1] == "'":
                current.append("''")
                index += 2
                continue
            in_single = False
            current.append(char)
            index += 1
            continue
        if char == ";" and not in_single:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1

    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    if in_single:
        msg = "migration SQL has an unclosed string literal"
        raise DatabaseMigrationError(msg)
    return statements


def get_migration_status(
    database_path: Path | str,
    *,
    migrations: Sequence[Migration] | None = None,
) -> MigrationStatus:
    """Inspect migration state using a read-only connection when the file exists."""
    path = Path(database_path)
    packaged = tuple(migrations) if migrations is not None else discover_migrations()
    latest_version = packaged[-1].version if packaged else 0

    if not path.exists():
        return MigrationStatus(
            database_path=path,
            current_version=0,
            latest_version=latest_version,
            applied=(),
            pending=packaged,
            checksums_valid=True,
            is_up_to_date=latest_version == 0,
        )

    with connect_database(path, read_only=True) as connection:
        applied = _read_applied_migrations(connection)
        _verify_applied_history(applied, packaged)
        current_version = applied[-1].version if applied else 0
        pending = tuple(item for item in packaged if item.version > current_version)
        return MigrationStatus(
            database_path=path.resolve(),
            current_version=current_version,
            latest_version=latest_version,
            applied=applied,
            pending=pending,
            checksums_valid=True,
            is_up_to_date=current_version == latest_version and not pending,
        )


def apply_migrations(
    database_path: Path | str,
    *,
    migrations: Sequence[Migration] | None = None,
) -> DatabaseReadiness:
    """Apply pending packaged migrations under a write lock."""
    path = Path(database_path)
    packaged = tuple(migrations) if migrations is not None else discover_migrations()
    applied_now: list[Migration] = []
    previous_version = 0

    try:
        with connect_database(path) as connection:
            with transaction(connection, immediate=True):
                _ensure_schema_migrations_table(connection)
                applied = _read_applied_migrations(connection)
                _verify_applied_history(applied, packaged)
                previous_version = applied[-1].version if applied else 0
                pending = [item for item in packaged if item.version > previous_version]
                for migration in pending:
                    _apply_one_migration(connection, migration)
                    applied_now.append(migration)
                final_applied = _read_applied_migrations(connection)
                final_version = final_applied[-1].version if final_applied else 0
                latest_version = packaged[-1].version if packaged else 0
                if final_version != latest_version:
                    msg = (
                        f"migration did not reach latest version "
                        f"(current={final_version}, latest={latest_version})"
                    )
                    raise DatabaseMigrationError(msg)
    except DatabaseMigrationError:
        raise
    except DatabaseConnectionError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            msg = f"database is busy while applying migrations at {path}: {exc}"
            raise DatabaseMigrationError(msg) from exc
        raise
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            msg = f"database is busy while applying migrations at {path}: {exc}"
            raise DatabaseMigrationError(msg) from exc
        msg = f"migration failed for {path}: {exc}"
        raise DatabaseMigrationError(msg) from exc
    except sqlite3.Error as exc:
        msg = f"migration failed for {path}: {exc}"
        raise DatabaseMigrationError(msg) from exc

    status = get_migration_status(path, migrations=packaged)
    return DatabaseReadiness(
        database_path=path.resolve(),
        previous_version=previous_version,
        schema_version=status.current_version,
        migrations_applied=tuple(applied_now),
        status=status,
    )


def ensure_database_ready(
    database_path: Path | str,
    *,
    migrations: Sequence[Migration] | None = None,
) -> DatabaseReadiness:
    """Open a writable connection, verify history, apply pending migrations, and close."""
    return apply_migrations(database_path, migrations=migrations)


def format_database_status(status: MigrationStatus) -> str:
    """Return concise human-readable migration status."""
    return (
        "database valid: "
        f"path={status.database_path} "
        f"current_version={status.current_version} "
        f"latest_version={status.latest_version} "
        f"pending={len(status.pending)}"
    )


def _normalize_migration_text(sql_text: str) -> str:
    return sql_text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_line_comments(sql_text: str) -> str:
    lines: list[str] = []
    for line in _normalize_migration_text(sql_text).split("\n"):
        in_single = False
        result_chars: list[str] = []
        index = 0
        while index < len(line):
            char = line[index]
            if char == "'" and not in_single:
                in_single = True
                result_chars.append(char)
                index += 1
                continue
            if char == "'" and in_single:
                if index + 1 < len(line) and line[index + 1] == "'":
                    result_chars.append("''")
                    index += 2
                    continue
                in_single = False
                result_chars.append(char)
                index += 1
                continue
            if char == "-" and not in_single and index + 1 < len(line) and line[index + 1] == "-":
                break
            result_chars.append(char)
            index += 1
        lines.append("".join(result_chars))
    return "\n".join(lines)


def _ensure_schema_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA_MIGRATIONS_DDL)


def _read_applied_migrations(connection: sqlite3.Connection) -> tuple[AppliedMigration, ...]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SCHEMA_MIGRATIONS_TABLE,),
    ).fetchone()
    if exists is None:
        return ()
    rows = connection.execute(
        f"""
        SELECT version, name, checksum, applied_at, execution_time_ms
        FROM {SCHEMA_MIGRATIONS_TABLE}
        ORDER BY version ASC
        """
    ).fetchall()
    applied: list[AppliedMigration] = []
    for row in rows:
        applied.append(
            AppliedMigration(
                version=int(row["version"]),
                name=str(row["name"]),
                checksum=str(row["checksum"]),
                applied_at=parse_utc_timestamp(str(row["applied_at"])),
                execution_time_ms=int(row["execution_time_ms"]),
            )
        )
    return tuple(applied)


def _verify_applied_history(
    applied: Sequence[AppliedMigration],
    packaged: Sequence[Migration],
) -> None:
    packaged_by_version = {item.version: item for item in packaged}
    latest_packaged = packaged[-1].version if packaged else 0
    for record in applied:
        if record.version > latest_packaged:
            msg = (
                f"database schema version {record.version} is newer than "
                f"packaged migrations (latest={latest_packaged})"
            )
            raise DatabaseMigrationError(msg)
        packaged_migration = packaged_by_version.get(record.version)
        if packaged_migration is None:
            msg = f"applied migration version {record.version} is missing from the package"
            raise DatabaseMigrationError(msg)
        if packaged_migration.name != record.name:
            msg = (
                f"applied migration version {record.version} name mismatch: "
                f"stored={record.name!r} packaged={packaged_migration.name!r}"
            )
            raise DatabaseMigrationError(msg)
        if packaged_migration.checksum != record.checksum:
            msg = (
                f"applied migration version {record.version} checksum mismatch: "
                f"stored={record.checksum} packaged={packaged_migration.checksum}"
            )
            raise DatabaseMigrationError(msg)


def _apply_one_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    validate_migration_sql(migration.sql_text, filename=migration.filename)
    started = time.perf_counter()
    try:
        for statement in split_sql_statements(migration.sql_text):
            connection.execute(statement)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        connection.execute(
            f"""
            INSERT INTO {SCHEMA_MIGRATIONS_TABLE}
                (version, name, checksum, applied_at, execution_time_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                migration.version,
                migration.name,
                migration.checksum,
                format_utc_timestamp(utc_now()),
                elapsed_ms,
            ),
        )
    except sqlite3.Error as exc:
        msg = f"failed applying migration {migration.filename}: {exc}"
        raise DatabaseMigrationError(msg) from exc

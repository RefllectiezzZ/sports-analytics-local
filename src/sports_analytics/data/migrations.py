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
_MIGRATION_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CHECKSUM_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_PROHIBITED_FIRST_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "BEGIN",
        "COMMIT",
        "END",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "VACUUM",
        "ATTACH",
        "DETACH",
        "PRAGMA",
    }
)
_SCHEMA_MIGRATIONS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    execution_time_ms INTEGER NOT NULL,
    CHECK (typeof(version) = 'integer'),
    CHECK (version >= 1),
    CHECK (length(name) > 0),
    CHECK (length(checksum) = 64),
    CHECK (typeof(execution_time_ms) = 'integer'),
    CHECK (execution_time_ms >= 0)
)
"""


def discover_migrations(
    *,
    package: str = "sports_analytics.data.sql.migrations",
) -> tuple[Migration, ...]:
    """Discover packaged migrations deterministically by numeric version."""
    try:
        root = resources.files(package)
    except (ModuleNotFoundError, TypeError, OSError) as exc:
        msg = f"unable to locate migration package {package!r}: {exc}"
        raise DatabaseMigrationError(msg) from exc

    discovered: list[Migration] = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        msg = f"failed enumerating migration package {package!r}: {exc}"
        raise DatabaseMigrationError(msg) from exc

    for entry in entries:
        try:
            is_file = entry.is_file()
            name = entry.name
        except OSError as exc:
            msg = f"failed inspecting migration resource in package {package!r}: {exc}"
            raise DatabaseMigrationError(msg) from exc
        if not is_file:
            continue
        if name == "__init__.py" or name.startswith("."):
            continue
        match = _MIGRATION_FILENAME_PATTERN.fullmatch(name)
        if match is None:
            msg = f"malformed migration filename rejected: {name!r}"
            raise DatabaseMigrationError(msg)
        version = int(match.group("version"))
        migration_name = match.group("name")
        try:
            sql_text = entry.read_text(encoding="utf-8")
        except UnicodeError as exc:
            msg = (
                f"failed reading migration {name!r} from package {package!r}: "
                f"invalid UTF-8 encoding ({exc})"
            )
            raise DatabaseMigrationError(msg) from exc
        except OSError as exc:
            msg = f"failed reading migration {name!r} from package {package!r}: {exc}"
            raise DatabaseMigrationError(msg) from exc
        checksum = compute_migration_checksum(sql_text)
        discovered.append(
            Migration(
                version=version,
                name=migration_name,
                sql_text=sql_text,
                checksum=checksum,
                filename=name,
            )
        )
    return validate_migration_sequence(discovered)


def validate_migration_sequence(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    """Validate and return a deterministic immutable migration sequence."""
    if not migrations:
        return ()

    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    seen_versions: set[int] = set()
    for migration in ordered:
        if type(migration.version) is not int or migration.version < 1:
            msg = f"migration version must be a positive integer, got {migration.version!r}"
            raise DatabaseMigrationError(msg)
        if migration.version in seen_versions:
            msg = f"duplicate migration version detected: {migration.version}"
            raise DatabaseMigrationError(msg)
        seen_versions.add(migration.version)
        if not _MIGRATION_NAME_PATTERN.fullmatch(migration.name):
            msg = f"invalid migration name: {migration.name!r}"
            raise DatabaseMigrationError(msg)
        expected_filename = f"{migration.version:04d}_{migration.name}.sql"
        if migration.filename != expected_filename:
            msg = (
                f"migration filename/version/name mismatch: "
                f"filename={migration.filename!r} expected={expected_filename!r}"
            )
            raise DatabaseMigrationError(msg)
        if not _CHECKSUM_PATTERN.fullmatch(migration.checksum):
            msg = f"migration checksum must be 64 lowercase hex characters: {migration.filename}"
            raise DatabaseMigrationError(msg)
        expected_checksum = compute_migration_checksum(migration.sql_text)
        if migration.checksum != expected_checksum:
            msg = (
                f"migration checksum inconsistent with SQL text for {migration.filename}: "
                f"stored={migration.checksum} computed={expected_checksum}"
            )
            raise DatabaseMigrationError(msg)
        validate_migration_sql(migration.sql_text, filename=migration.filename)

    expected_versions = list(range(1, ordered[-1].version + 1))
    actual_versions = [item.version for item in ordered]
    if actual_versions != expected_versions:
        msg = (
            "migration versions must be consecutive starting at 1; "
            f"found {actual_versions}, expected {expected_versions}"
        )
        raise DatabaseMigrationError(msg)
    return ordered


def compute_migration_checksum(sql_text: str) -> str:
    """Return the SHA-256 checksum of normalized migration SQL text."""
    normalized = _normalize_migration_text(sql_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_migration_sql(sql_text: str, *, filename: str) -> None:
    """Reject prohibited transaction-control or unsafe statements.

    All packaged migration PRAGMA statements are prohibited. Connection safety
    PRAGMAs belong in ``database.py``, not migration SQL.
    """
    for statement in split_sql_statements(sql_text):
        token = _first_sql_token(statement)
        if token is None:
            continue
        upper = token.upper()
        if upper in _PROHIBITED_FIRST_TOKENS:
            msg = f"prohibited statement in migration {filename}: {upper}"
            raise DatabaseMigrationError(msg)


def split_sql_statements(sql_text: str) -> list[str]:
    """Split migration SQL into executable statements without using executescript.

    Uses a quote/comment-aware scanner and ``sqlite3.complete_statement`` so
    compound statements (for example ``CREATE TRIGGER ... BEGIN ... END;``) are
    supported when their internal semicolons do not yet complete the statement.
    Comments are skipped rather than buffered so trailing comment-only content
    cannot hide a missing terminator or leave an incomplete statement.
    Parenthesis depth is tracked so heuristically "complete" fragments such as
    ``CREATE TABLE a(;`` are still rejected as incomplete.
    """
    text = _normalize_migration_text(sql_text)
    if text.startswith("\ufeff"):
        text = text[1:]
    statements: list[str] = []
    current: list[str] = []
    state = "normal"
    paren_depth = 0
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""

        if state == "normal":
            if char == "-" and nxt == "-":
                state = "line_comment"
                index += 2
                continue
            if char == "/" and nxt == "*":
                state = "block_comment"
                index += 2
                continue
            if char == "'":
                state = "single"
                current.append(char)
                index += 1
                continue
            if char == '"':
                state = "double"
                current.append(char)
                index += 1
                continue
            if char == "`":
                state = "backtick"
                current.append(char)
                index += 1
                continue
            if char == "[":
                state = "bracket"
                current.append(char)
                index += 1
                continue
            if char == "(":
                paren_depth += 1
                current.append(char)
                index += 1
                continue
            if char == ")":
                if paren_depth > 0:
                    paren_depth -= 1
                current.append(char)
                index += 1
                continue
            if char == ";":
                current.append(char)
                buffer = "".join(current)
                if paren_depth == 0 and sqlite3.complete_statement(buffer):
                    candidate = buffer.strip()
                    if candidate:
                        # Keep executable statement text without a trailing semicolon.
                        statements.append(candidate.rstrip().rstrip(";").strip())
                    current = []
                # Incomplete after ';' (e.g. trigger body): keep accumulating.
                index += 1
                continue
            current.append(char)
            index += 1
            continue

        if state == "line_comment":
            index += 1
            if char == "\n":
                # Preserve a newline so statement text remains readable/complete.
                current.append("\n")
                state = "normal"
            continue

        if state == "block_comment":
            if char == "*" and nxt == "/":
                index += 2
                state = "normal"
                continue
            index += 1
            continue

        if state == "single":
            current.append(char)
            if char == "'" and nxt == "'":
                current.append(nxt)
                index += 2
                continue
            if char == "'":
                state = "normal"
            index += 1
            continue

        if state in {"double", "backtick"}:
            quote = '"' if state == "double" else "`"
            current.append(char)
            if char == quote and nxt == quote:
                current.append(nxt)
                index += 2
                continue
            if char == quote:
                state = "normal"
            index += 1
            continue

        if state == "bracket":
            current.append(char)
            index += 1
            if char == "]":
                state = "normal"
            continue

        msg = f"internal SQL parser entered unexpected state {state!r}"
        raise DatabaseMigrationError(msg)

    if state == "block_comment":
        msg = "migration SQL has an unclosed block comment"
        raise DatabaseMigrationError(msg)
    if state in {"single", "double", "backtick", "bracket"}:
        msg = "migration SQL has an unclosed quoted value or identifier"
        raise DatabaseMigrationError(msg)
    if paren_depth != 0:
        msg = "migration SQL ends with an incomplete statement"
        raise DatabaseMigrationError(msg)

    trailing = "".join(current).strip()
    if not trailing:
        # Trailing whitespace / comment-only content yields no statements.
        return statements

    if sqlite3.complete_statement(trailing):
        statements.append(trailing.rstrip(";").strip())
        return statements

    candidate = f"{trailing};"
    if sqlite3.complete_statement(candidate):
        statements.append(trailing.rstrip(";").strip())
        return statements

    msg = "migration SQL ends with an incomplete statement"
    raise DatabaseMigrationError(msg)


def get_migration_status(
    database_path: Path | str,
    *,
    migrations: Sequence[Migration] | None = None,
) -> MigrationStatus:
    """Inspect migration state using a read-only connection when the file exists."""
    path = Path(database_path)
    packaged = (
        validate_migration_sequence(migrations) if migrations is not None else discover_migrations()
    )
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
    packaged = (
        validate_migration_sequence(migrations) if migrations is not None else discover_migrations()
    )
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


def _first_sql_token(statement: str) -> str | None:
    """Return the first SQL token after whitespace, comments, and an optional BOM."""
    text = _normalize_migration_text(statement)
    if text.startswith("\ufeff"):
        text = text[1:]
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char.isspace():
            index += 1
            continue
        if char == "-" and nxt == "-":
            index += 2
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            if index + 1 >= length:
                return None
            index += 2
            continue
        start = index
        while index < length and not text[index].isspace() and text[index] not in ";()":
            index += 1
        token = text[start:index]
        return token or None
    return None


def _ensure_schema_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(_SCHEMA_MIGRATIONS_DDL)


def _read_applied_migrations(connection: sqlite3.Connection) -> tuple[AppliedMigration, ...]:
    try:
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
    except sqlite3.Error as exc:
        msg = "unable to read schema_migrations metadata"
        raise DatabaseMigrationError(msg) from exc

    applied: list[AppliedMigration] = []
    for row in rows:
        try:
            version_raw = row["version"]
            name = str(row["name"])
            checksum = str(row["checksum"])
            applied_at_raw = str(row["applied_at"])
            execution_raw = row["execution_time_ms"]
            if type(version_raw) is not int:
                msg = f"applied migration version must be an integer, got {version_raw!r}"
                raise DatabaseMigrationError(msg)
            if version_raw < 1:
                msg = f"applied migration version must be >= 1, got {version_raw}"
                raise DatabaseMigrationError(msg)
            if not name:
                msg = "applied migration name must be non-empty"
                raise DatabaseMigrationError(msg)
            if not _CHECKSUM_PATTERN.fullmatch(checksum):
                msg = f"applied migration checksum is malformed: {checksum!r}"
                raise DatabaseMigrationError(msg)
            if type(execution_raw) is not int:
                msg = (
                    f"applied migration execution_time_ms must be an integer, got {execution_raw!r}"
                )
                raise DatabaseMigrationError(msg)
            if execution_raw < 0:
                msg = f"applied migration execution_time_ms must be >= 0, got {execution_raw}"
                raise DatabaseMigrationError(msg)
            applied.append(
                AppliedMigration(
                    version=version_raw,
                    name=name,
                    checksum=checksum,
                    applied_at=parse_utc_timestamp(applied_at_raw),
                    execution_time_ms=execution_raw,
                )
            )
        except DatabaseMigrationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            msg = "malformed applied migration metadata in schema_migrations"
            raise DatabaseMigrationError(msg) from exc
    return tuple(applied)


def _verify_applied_history(
    applied: Sequence[AppliedMigration],
    packaged: Sequence[Migration],
) -> None:
    packaged_by_version = {item.version: item for item in packaged}
    latest_packaged = packaged[-1].version if packaged else 0
    if not applied:
        return

    versions = [record.version for record in applied]
    expected = list(range(1, versions[-1] + 1))
    if versions != expected:
        msg = (
            "applied migration history must be the exact consecutive prefix "
            f"1..N; found {versions}, expected {expected}"
        )
        raise DatabaseMigrationError(msg)

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

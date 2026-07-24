"""SQLite connection factory and explicit transaction helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from sports_analytics.core.exceptions import (
    DatabaseConnectionError,
    DatabaseError,
    RepositoryError,
)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_BUSY_TIMEOUT_MS: Final[int] = 30_000
SQLITE_HEADER: Final[bytes] = b"SQLite format 3\x00"


def read_sqlite_header(database_path: Path | str) -> bytes:
    """Read exactly the SQLite header bytes without loading the full file."""
    path = Path(database_path)
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as exc:
        msg = f"unable to read SQLite database header at {path}: {exc}"
        raise DatabaseConnectionError(msg) from exc
    return header


def require_active_transaction(
    connection: sqlite3.Connection,
    *,
    operation: str,
) -> None:
    """Require an explicit caller-owned write transaction before mutation."""
    if not connection.in_transaction:
        msg = (
            f"operation {operation!r} requires an active explicit transaction; "
            "callers must use transaction(...)"
        )
        raise RepositoryError(msg)


@contextmanager
def connect_database(
    database_path: Path | str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    read_only: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Open an explicitly owned SQLite connection and close it on exit.

    Opening and configuration errors are converted to ``DatabaseConnectionError``.
    Exceptions raised by caller code inside the ``with`` body propagate unchanged.
    """
    path = Path(database_path)
    if path.exists() and path.is_dir():
        msg = f"SQLite path points to a directory: {path}"
        raise DatabaseConnectionError(msg)

    if read_only:
        connection = _open_read_only(path, timeout_seconds=timeout_seconds)
    else:
        connection = _open_writable(path, timeout_seconds=timeout_seconds)

    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Run a caller-owned unit of work with explicit commit/rollback.

    Nested independent transactions are rejected. Repository methods must not
    call ``commit`` when the caller owns the transaction boundary.
    """
    if connection.in_transaction:
        msg = "unsupported nested transaction: connection already has an open transaction"
        raise DatabaseError(msg)

    begin_sql = "BEGIN IMMEDIATE" if immediate else "BEGIN"
    try:
        connection.execute(begin_sql)
        yield connection
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    else:
        connection.commit()


def verify_sqlite_file(database_path: Path | str, *, quick: bool = True) -> None:
    """Verify that ``database_path`` is an existing SQLite database file."""
    path = Path(database_path)
    if not path.exists():
        msg = f"SQLite database file does not exist: {path}"
        raise DatabaseConnectionError(msg)
    if path.is_dir():
        msg = f"SQLite path points to a directory: {path}"
        raise DatabaseConnectionError(msg)
    header = read_sqlite_header(path)
    if header != SQLITE_HEADER:
        msg = f"file is not a valid SQLite database: {path}"
        raise DatabaseConnectionError(msg)

    pragma = "PRAGMA quick_check" if quick else "PRAGMA integrity_check"
    try:
        with connect_database(path, read_only=True) as connection:
            row = connection.execute(pragma).fetchone()
            result = str(row[0]) if row is not None else "unknown"
            if result.lower() != "ok":
                msg = f"SQLite integrity check failed for {path}: {result}"
                raise DatabaseConnectionError(msg)
    except DatabaseConnectionError:
        raise
    except sqlite3.Error as exc:
        msg = f"SQLite integrity check failed for {path}: {exc}"
        raise DatabaseConnectionError(msg) from exc


def _open_writable(path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"unable to create SQLite parent directory {path.parent}: {exc}"
        raise DatabaseConnectionError(msg) from exc

    if path.exists():
        header = read_sqlite_header(path)
        if header and header != SQLITE_HEADER:
            msg = f"refusing to open non-SQLite file as database: {path}"
            raise DatabaseConnectionError(msg)

    try:
        connection = sqlite3.connect(
            str(path),
            timeout=timeout_seconds,
            isolation_level=None,
            check_same_thread=True,
        )
    except sqlite3.Error as exc:
        msg = f"unable to open SQLite database at {path}: {exc}"
        raise DatabaseConnectionError(msg) from exc

    try:
        _configure_writable_connection(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _open_read_only(path: Path, *, timeout_seconds: float) -> sqlite3.Connection:
    if not path.exists():
        msg = f"SQLite database file does not exist: {path}"
        raise DatabaseConnectionError(msg)
    if path.is_dir():
        msg = f"SQLite path points to a directory: {path}"
        raise DatabaseConnectionError(msg)
    header = read_sqlite_header(path)
    if header != SQLITE_HEADER:
        msg = f"file is not a valid SQLite database: {path}"
        raise DatabaseConnectionError(msg)

    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=timeout_seconds,
            isolation_level=None,
            check_same_thread=True,
        )
    except sqlite3.Error as exc:
        msg = f"unable to open read-only SQLite database at {path}: {exc}"
        raise DatabaseConnectionError(msg) from exc

    try:
        _configure_read_only_connection(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _configure_writable_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        journal_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        journal_mode = str(journal_row[0]).lower() if journal_row is not None else ""
        if journal_mode != "wal":
            msg = f"failed to enable WAL journal mode (got {journal_mode!r})"
            raise DatabaseConnectionError(msg)
        connection.execute("PRAGMA synchronous = NORMAL")
        fk_row = connection.execute("PRAGMA foreign_keys").fetchone()
        if fk_row is None or int(fk_row[0]) != 1:
            msg = "foreign keys were not enabled on the SQLite connection"
            raise DatabaseConnectionError(msg)
    except DatabaseConnectionError:
        raise
    except sqlite3.Error as exc:
        msg = f"failed to configure writable SQLite connection: {exc}"
        raise DatabaseConnectionError(msg) from exc


def _configure_read_only_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        fk_row = connection.execute("PRAGMA foreign_keys").fetchone()
        if fk_row is None or int(fk_row[0]) != 1:
            msg = "foreign keys were not enabled on the read-only SQLite connection"
            raise DatabaseConnectionError(msg)
    except DatabaseConnectionError:
        raise
    except sqlite3.Error as exc:
        msg = f"failed to configure read-only SQLite connection: {exc}"
        raise DatabaseConnectionError(msg) from exc

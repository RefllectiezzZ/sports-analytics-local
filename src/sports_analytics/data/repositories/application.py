"""Application metadata repository."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from sports_analytics.core.exceptions import DatabaseIntegrityError, RepositoryError
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.schema import APPLICATION_METADATA_TABLE
from sports_analytics.data.types import validate_metadata_key, validate_plain_text


class ApplicationMetadataRepository:
    """Typed key/value metadata access using an explicit connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, key: str) -> str | None:
        """Return the stored value for ``key``, or ``None`` when absent."""
        normalized = validate_metadata_key(key)
        try:
            row = self._connection.execute(
                f"SELECT value FROM {APPLICATION_METADATA_TABLE} WHERE key = ?",
                (normalized,),
            ).fetchone()
        except sqlite3.Error as exc:
            msg = f"failed to read application metadata key {normalized!r}"
            raise RepositoryError(msg) from exc
        if row is None:
            return None
        return str(row["value"])

    def upsert(self, key: str, value: str, updated_at: datetime) -> None:
        """Insert or replace a metadata value without committing."""
        normalized_key = validate_metadata_key(key)
        normalized_value = validate_plain_text(value, field_name="value")
        timestamp = format_utc_timestamp(updated_at)
        try:
            self._connection.execute(
                f"""
                INSERT INTO {APPLICATION_METADATA_TABLE} (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (normalized_key, normalized_value, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            msg = f"integrity error upserting application metadata key {normalized_key!r}"
            raise DatabaseIntegrityError(msg) from exc
        except sqlite3.Error as exc:
            msg = f"failed to upsert application metadata key {normalized_key!r}"
            raise RepositoryError(msg) from exc

    def list_all(self) -> list[tuple[str, str, datetime]]:
        """Return all metadata rows ordered by key."""
        try:
            rows = self._connection.execute(
                f"""
                SELECT key, value, updated_at
                FROM {APPLICATION_METADATA_TABLE}
                ORDER BY key ASC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            msg = "failed to list application metadata"
            raise RepositoryError(msg) from exc
        return [
            (str(row["key"]), str(row["value"]), parse_utc_timestamp(str(row["updated_at"])))
            for row in rows
        ]

"""Tests for application metadata repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.application import ApplicationMetadataRepository

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)


def test_application_metadata_crud_and_rollback(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = ApplicationMetadataRepository(connection)
        assert repo.get("missing") is None
        with transaction(connection):
            repo.upsert("alpha", "", FIXED)
            repo.upsert("beta", "two", FIXED)
        assert repo.get("alpha") == ""
        assert repo.get("missing") is None
        listed = repo.list_all()
        assert [item[0] for item in listed] == ["alpha", "beta"]

        with transaction(connection):
            repo.upsert("alpha", "changed", FIXED.replace(microsecond=1))
        assert repo.get("alpha") == "changed"
        assert listed[0][2] == FIXED

        with pytest.raises(RuntimeError):
            with transaction(connection):
                repo.upsert("alpha", "rolled", FIXED.replace(microsecond=2))
                raise RuntimeError("rollback")
        assert repo.get("alpha") == "changed"

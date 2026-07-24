"""Tests for append-only audit event repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import RepositoryError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.audit import AuditEventRepository

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)


def test_audit_append_list_filters_and_rollback(tmp_path: Path) -> None:
    db = tmp_path / "ops.sqlite3"
    ensure_database_ready(db)
    with connect_database(db) as connection:
        repo = AuditEventRepository(connection)
        assert not hasattr(repo, "update_event")
        assert not hasattr(repo, "delete_event")
        with transaction(connection):
            first = repo.append_event(
                event_type="config.loaded",
                entity_type="settings",
                entity_id="local",
                actor="cli",
                details={"b": 1, "a": 2},
                occurred_at=FIXED,
                correlation_id="corr-1",
            )
            second = repo.append_event(
                event_type="job.created",
                entity_type="job",
                entity_id="j1",
                actor="worker",
                details={"ok": True},
                occurred_at=FIXED.replace(microsecond=1),
                correlation_id="corr-2",
            )
        assert dumps_canonical_json(first.details) == '{"a":2,"b":1}'
        loaded = repo.get_event(first.id)
        assert loaded is not None and loaded.id == first.id
        listed = repo.list_events(event_type="job.created")
        assert [item.id for item in listed] == [second.id]
        ranged = repo.list_events(
            occurred_from=FIXED,
            occurred_to=FIXED,
        )
        assert [item.id for item in ranged] == [first.id]
        ordered = repo.list_events()
        assert [item.id for item in ordered] == [second.id, first.id]
        with pytest.raises(RepositoryError):
            repo.list_events(offset=-1)

        with pytest.raises(RuntimeError):
            with transaction(connection):
                repo.append_event(
                    event_type="temp.event",
                    entity_type="temp",
                    actor="cli",
                    details={},
                    occurred_at=FIXED.replace(microsecond=2),
                )
                raise RuntimeError("rollback")
        assert repo.get_event(second.id + 1) is None

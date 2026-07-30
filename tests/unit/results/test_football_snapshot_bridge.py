from __future__ import annotations

from datetime import UTC, datetime

from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.operations import ResultSnapshotRegistrationRepository
from sports_analytics.results.football_snapshot_bridge import (
    register_completed_results_from_snapshot,
)
from tests.helpers_snapshots import (
    SYNTHETIC_CSV_WITH_ODDS,
    database_path,
    prepare,
    publication_service,
)


def test_verified_completed_event_registers_idempotently_and_scheduled_does_not(
    tmp_path,
) -> None:
    database = database_path(tmp_path)
    snapshots = tmp_path / "snapshots"
    prepared = prepare(
        tmp_path,
        snapshot_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        snapshots_directory=snapshots,
        content=SYNTHETIC_CSV_WITH_ODDS,
    )
    published = publication_service(database, snapshots).publish_or_reuse(
        prepared,
        actor="test",
    )
    first = register_completed_results_from_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        relative_manifest_path=published.snapshot_relative_path,
        output_relative_root="canonical-results/from-football-snapshot",
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        actor="test",
    )
    second = register_completed_results_from_snapshot(
        database_path=database,
        snapshots_directory=snapshots,
        relative_manifest_path=published.snapshot_relative_path,
        output_relative_root="canonical-results/from-football-snapshot",
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        actor="test",
    )
    assert first.completed_events == 1
    assert first.skipped_events == 0
    assert second.result_snapshots == first.result_snapshots
    with connect_database(database, read_only=True) as connection:
        registered = ResultSnapshotRegistrationRepository(connection).list_registered()
    assert len(registered) == 1
    assert registered[0]["event_status"] == "completed"

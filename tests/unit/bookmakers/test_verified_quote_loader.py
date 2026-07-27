"""Loader-bound verified quote evidence tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sports_analytics.bookmakers.loader import load_verified_bookmaker_quotes
from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.database import connect_database
from tests.unit.bookmakers.test_bookmaker_loader import _published_snapshot

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_loader_returns_verified_quotes_by_observation_id(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    with connect_database(database) as connection:
        loaded = load_verified_bookmaker_quotes(
            database_connection=connection,
            snapshots_directory=snapshots,
            raw_directory=raw,
            snapshot_id=snapshot_id,
        )
    assert loaded.verified is True
    assert len(loaded.verified_quotes_by_observation_id) == loaded.quote_count
    observation_id, quote = loaded.verified_quotes_by_observation_id[0]
    assert quote.snapshot_id == snapshot_id
    assert quote.canonical_market_definition_id.startswith("football-")
    assert observation_id == quote.identity.quote_observation_id


def test_loader_rejects_tampered_quote_checksum(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    with connect_database(database) as connection:
        loaded = load_verified_bookmaker_quotes(
            database_connection=connection,
            snapshots_directory=snapshots,
            raw_directory=raw,
            snapshot_id=snapshot_id,
        )
    quotes_path = next(snapshots.rglob("market_quotes.parquet"))
    import pyarrow.parquet as pq

    table = pq.read_table(quotes_path)
    rows = table.to_pylist()
    rows[0]["source_file_sha256"] = "f" * 64
    pq.write_table(__import__("pyarrow").Table.from_pylist(rows, schema=table.schema), quotes_path)
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )
    _ = loaded


def test_snapshot_id_string_alone_is_insufficient_without_loader(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id="00000000-0000-0000-0000-000000000000",
            )

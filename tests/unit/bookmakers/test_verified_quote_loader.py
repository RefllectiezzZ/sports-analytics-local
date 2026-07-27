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


def _rewrite_parquet(path: Path, mutator) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    rows = table.to_pylist()
    mutator(rows)
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)


def test_loader_rejects_tampered_source_participant_provider(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("source_participants.parquet"))
    _rewrite_parquet(path, lambda rows: rows[0].__setitem__("source_name", "other-provider"))
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="provider identity mismatch|checksum"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_tampered_source_event_sport(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("source_events.parquet"))
    _rewrite_parquet(path, lambda rows: rows[0].__setitem__("sport_code", "tennis"))
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="sport mismatch|checksum"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_tampered_eligibility_line(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("comparison_eligibility.parquet"))
    _rewrite_parquet(path, lambda rows: rows[0].__setitem__("line_value", "99.5"))
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="eligibility|checksum"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_tampered_canonical_event_participants(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("canonical_events.parquet"))
    _rewrite_parquet(
        path,
        lambda rows: rows[0].__setitem__("home_canonical_participant_id", "tampered-home"),
    )
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="participant|checksum|mismatch"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_tampered_quote_source_event_id(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("market_quotes.parquet"))
    _rewrite_parquet(path, lambda rows: rows[0].__setitem__("source_event_id", "missing-source"))
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="source_event_id|checksum"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_source_event_checksum_not_in_manifest(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("source_events.parquet"))
    _rewrite_parquet(path, lambda rows: rows[0].__setitem__("source_file_sha256", "a" * 64))
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="source_file_sha256|checksum"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_participant_reconciliation_home_mismatch(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("participant_reconciliations.parquet"))

    def _swap_home(rows: list[dict]) -> None:
        rows[0]["canonical_participant_id"] = "tampered-participant"

    _rewrite_parquet(path, _swap_home)
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="participant|checksum|mismatch"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_rejects_missing_market_status(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    path = next(snapshots.rglob("market_quotes.parquet"))
    _rewrite_parquet(path, lambda rows: rows[0].__setitem__("market_status", ""))
    with connect_database(database) as connection:
        with pytest.raises(SnapshotVerificationError, match="market_status|checksum"):
            load_verified_bookmaker_quotes(
                database_connection=connection,
                snapshots_directory=snapshots,
                raw_directory=raw,
                snapshot_id=snapshot_id,
            )


def test_loader_exposes_catalogue_not_public_constructor(tmp_path: Path) -> None:
    database, raw, snapshots, snapshot_id = _published_snapshot(tmp_path)
    with connect_database(database) as connection:
        loaded = load_verified_bookmaker_quotes(
            database_connection=connection,
            snapshots_directory=snapshots,
            raw_directory=raw,
            snapshot_id=snapshot_id,
        )
    assert loaded.catalogue is not None
    assert loaded.catalogue.snapshot_id == snapshot_id
    assert "build_verified_quote_from_loaded_row" not in dir(
        __import__("sports_analytics.bookmakers.verified_evidence", fromlist=["*"])
    )
    from sports_analytics.bookmakers import loader as loader_mod

    assert hasattr(loader_mod, "_build_verified_quote_from_loaded_row")
    assert not hasattr(loader_mod, "build_verified_quote_from_loaded_row")

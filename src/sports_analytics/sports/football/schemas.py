"""Football snapshot dataset suite and football-specific Arrow schema.

The football ingestion snapshot combines shared canonical and source-scoped
datasets with one football-specific post-match statistics dataset.
"""

from __future__ import annotations

from typing import Any, Final

import pyarrow as pa

from sports_analytics.markets.schemas import (
    DATASET_MARKET_QUOTES,
    market_quote_rows,
    market_quotes_schema,
)
from sports_analytics.snapshots.arrow import (
    dataset_metadata,
    dictionary_string,
    utc_timestamp,
)
from sports_analytics.snapshots.arrow import (
    schema_fingerprint as schema_fingerprint,
)
from sports_analytics.snapshots.spec import DatasetDescriptor, SnapshotDatasetSuite
from sports_analytics.sports.football.contracts import (
    DATASET_POST_MATCH_STATISTICS,
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
)
from sports_analytics.sports.football.normalization import (
    NormalizedFootballBundle,
    PostMatchStatisticsRecord,
)
from sports_analytics.sports.identifiers import SPORT_FOOTBALL
from sports_analytics.sports.schemas import (
    DATASET_COMPETITIONS,
    DATASET_EVENT_RECONCILIATIONS,
    DATASET_EVENTS,
    DATASET_PARTICIPANT_RECONCILIATIONS,
    DATASET_PARTICIPANTS,
    DATASET_SEASONS,
    DATASET_SOURCE_EVENTS,
    DATASET_SOURCE_PARTICIPANTS,
    competition_rows,
    competitions_schema,
    event_reconciliation_rows,
    event_reconciliations_schema,
    event_rows,
    events_schema,
    participant_reconciliation_rows,
    participant_reconciliations_schema,
    participant_rows,
    participants_schema,
    season_rows,
    seasons_schema,
    source_event_rows,
    source_events_schema,
    source_participant_rows,
    source_participants_schema,
)


def post_match_statistics_schema() -> pa.Schema:
    """Return the football post-match statistics schema.

    Every field except identity and ``availability_stage`` is nullable because the
    source publishes statistics inconsistently across seasons. ``availability_stage``
    is always ``post-match`` so downstream readers cannot treat these values as
    pre-match features.
    """
    return pa.schema(
        [
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("availability_stage", dictionary_string(), nullable=False),
            pa.field("half_time_home_goals", pa.int16(), nullable=True),
            pa.field("half_time_away_goals", pa.int16(), nullable=True),
            pa.field("half_time_result", dictionary_string(), nullable=True),
            pa.field("referee", pa.string(), nullable=True),
            pa.field("home_shots", pa.int16(), nullable=True),
            pa.field("away_shots", pa.int16(), nullable=True),
            pa.field("home_shots_on_target", pa.int16(), nullable=True),
            pa.field("away_shots_on_target", pa.int16(), nullable=True),
            pa.field("home_corners", pa.int16(), nullable=True),
            pa.field("away_corners", pa.int16(), nullable=True),
            pa.field("home_fouls", pa.int16(), nullable=True),
            pa.field("away_fouls", pa.int16(), nullable=True),
            pa.field("home_yellow_cards", pa.int16(), nullable=True),
            pa.field("away_yellow_cards", pa.int16(), nullable=True),
            pa.field("home_red_cards", pa.int16(), nullable=True),
            pa.field("away_red_cards", pa.int16(), nullable=True),
            pa.field("source_observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_POST_MATCH_STATISTICS,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
            domain=SPORT_FOOTBALL,
        ),
    )


def post_match_statistics_rows(
    records: tuple[PostMatchStatisticsRecord, ...],
) -> list[dict[str, Any]]:
    """Build football post-match statistics rows in deterministic order."""
    return [
        {
            "canonical_event_id": item.canonical_event_id,
            "source_event_id": item.source_event_id,
            "availability_stage": item.availability_stage,
            "half_time_home_goals": item.half_time_home_goals,
            "half_time_away_goals": item.half_time_away_goals,
            "half_time_result": item.half_time_result,
            "referee": item.referee,
            "home_shots": item.home_shots,
            "away_shots": item.away_shots,
            "home_shots_on_target": item.home_shots_on_target,
            "away_shots_on_target": item.away_shots_on_target,
            "home_corners": item.home_corners,
            "away_corners": item.away_corners,
            "home_fouls": item.home_fouls,
            "away_fouls": item.away_fouls,
            "home_yellow_cards": item.home_yellow_cards,
            "away_yellow_cards": item.away_yellow_cards,
            "home_red_cards": item.home_red_cards,
            "away_red_cards": item.away_red_cards,
            "source_observed_at_utc": item.source_observed_at_utc,
            "source_file_sha256": item.source_file_sha256,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _build_suite() -> SnapshotDatasetSuite:
    version = FOOTBALL_CANONICAL_SCHEMA_VERSION
    descriptors = (
        DatasetDescriptor(
            dataset_name=DATASET_COMPETITIONS,
            relative_filename="competitions.parquet",
            schema=competitions_schema(schema_version=version, sport_code=SPORT_FOOTBALL),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_SEASONS,
            relative_filename="seasons.parquet",
            schema=seasons_schema(schema_version=version, sport_code=SPORT_FOOTBALL),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_PARTICIPANTS,
            relative_filename="participants.parquet",
            schema=participants_schema(schema_version=version, sport_code=SPORT_FOOTBALL),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_SOURCE_PARTICIPANTS,
            relative_filename="source_participants.parquet",
            schema=source_participants_schema(schema_version=version, sport_code=SPORT_FOOTBALL),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_PARTICIPANT_RECONCILIATIONS,
            relative_filename="participant_reconciliations.parquet",
            schema=participant_reconciliations_schema(
                schema_version=version,
                sport_code=SPORT_FOOTBALL,
            ),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_EVENTS,
            relative_filename="events.parquet",
            schema=events_schema(schema_version=version, sport_code=SPORT_FOOTBALL),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_SOURCE_EVENTS,
            relative_filename="source_events.parquet",
            schema=source_events_schema(schema_version=version, sport_code=SPORT_FOOTBALL),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_EVENT_RECONCILIATIONS,
            relative_filename="event_reconciliations.parquet",
            schema=event_reconciliations_schema(
                schema_version=version,
                sport_code=SPORT_FOOTBALL,
            ),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_MARKET_QUOTES,
            relative_filename="market_quotes.parquet",
            schema=market_quotes_schema(schema_version=version),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_POST_MATCH_STATISTICS,
            relative_filename="post_match_statistics.parquet",
            schema=post_match_statistics_schema(),
        ),
    )
    return SnapshotDatasetSuite(
        descriptors=descriptors,
        primary_dataset_name=DATASET_EVENTS,
    )


FOOTBALL_SNAPSHOT_SUITE: Final[SnapshotDatasetSuite] = _build_suite()

CANONICAL_DATASETS: Final[tuple[str, ...]] = FOOTBALL_SNAPSHOT_SUITE.dataset_names

PARQUET_FILENAMES: Final[dict[str, str]] = {
    descriptor.dataset_name: descriptor.relative_filename
    for descriptor in FOOTBALL_SNAPSHOT_SUITE.descriptors
}


def football_snapshot_suite() -> SnapshotDatasetSuite:
    """Return the immutable football ingestion dataset suite."""
    return FOOTBALL_SNAPSHOT_SUITE


def dataset_schema(dataset_name: str) -> pa.Schema:
    """Return the static schema for a named football snapshot dataset."""
    for descriptor in FOOTBALL_SNAPSHOT_SUITE.descriptors:
        if descriptor.dataset_name == dataset_name:
            return descriptor.schema
    msg = f"unknown football dataset schema: {dataset_name}"
    raise KeyError(msg)


def football_schema_fingerprints() -> dict[str, str]:
    """Return dataset name to schema fingerprint for the football suite."""
    return FOOTBALL_SNAPSHOT_SUITE.schema_fingerprints()


def bundle_to_tables(bundle: NormalizedFootballBundle) -> dict[str, pa.Table]:
    """Convert a normalized football bundle into explicitly typed Arrow tables."""
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {
        DATASET_COMPETITIONS: competition_rows(bundle.competitions),
        DATASET_SEASONS: season_rows(bundle.seasons),
        DATASET_PARTICIPANTS: participant_rows(bundle.participants),
        DATASET_SOURCE_PARTICIPANTS: source_participant_rows(bundle.participants),
        DATASET_PARTICIPANT_RECONCILIATIONS: participant_reconciliation_rows(
            bundle.participant_reconciliations
        ),
        DATASET_EVENTS: event_rows(tuple(item.canonical for item in bundle.events)),
        DATASET_SOURCE_EVENTS: source_event_rows(bundle.source_events),
        DATASET_EVENT_RECONCILIATIONS: event_reconciliation_rows(bundle.reconciliations),
        DATASET_MARKET_QUOTES: market_quote_rows(bundle.market_quotes),
        DATASET_POST_MATCH_STATISTICS: post_match_statistics_rows(bundle.post_match_statistics),
    }
    tables: dict[str, pa.Table] = {}
    for descriptor in FOOTBALL_SNAPSHOT_SUITE.descriptors:
        rows = rows_by_dataset[descriptor.dataset_name]
        tables[descriptor.dataset_name] = pa.Table.from_pylist(rows, schema=descriptor.schema)
    return tables

"""Arrow schemas and row builders for current-bookmaker-odds snapshots."""

from __future__ import annotations

from typing import Any, Final

import pyarrow as pa

from sports_analytics.bookmakers.native_inventory import (
    DATASET_PROVIDER_NATIVE_EVENTS,
    DATASET_PROVIDER_NATIVE_MARKETS,
    DATASET_PROVIDER_NATIVE_SELECTIONS,
    provider_native_events_schema,
    provider_native_markets_schema,
    provider_native_rows,
    provider_native_selections_schema,
)
from sports_analytics.bookmakers.normalization import (
    AcquisitionMetadataRecord,
    ComparisonEligibilityRecord,
    NormalizedBookmakerBundle,
    ParserDriftFinding,
)
from sports_analytics.bookmakers.status import ProviderStatusRecord
from sports_analytics.bookmakers.types import (
    BOOKMAKER_SCHEMA_VERSION,
    BOOKMAKER_SCHEMA_VERSION_V2,
)
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
from sports_analytics.snapshots.spec import DatasetDescriptor, SnapshotDatasetSuite
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sports.schemas import (
    DATASET_EVENT_RECONCILIATIONS,
    DATASET_PARTICIPANT_RECONCILIATIONS,
    DATASET_SOURCE_EVENTS,
    DATASET_SOURCE_PARTICIPANTS,
    event_reconciliation_rows,
    event_reconciliations_schema,
    event_rows,
    events_schema,
    participant_reconciliation_rows,
    participant_reconciliations_schema,
    source_event_rows,
    source_events_schema,
    source_participant_rows,
    source_participants_schema,
)

DATASET_ACQUISITION_METADATA: Final[str] = "acquisition_metadata"
DATASET_PROVIDER_STATUS: Final[str] = "provider_status"
DATASET_PARSER_DRIFT_FINDINGS: Final[str] = "parser_drift_findings"
DATASET_COMPARISON_ELIGIBILITY: Final[str] = "comparison_eligibility"
DATASET_CANONICAL_EVENTS: Final[str] = "canonical_events"
BOOKMAKERS_DOMAIN: Final[str] = "bookmakers"


def acquisition_metadata_schema(*, schema_version: str) -> pa.Schema:
    """Return the acquisition metadata dataset schema."""
    return pa.schema(
        [
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("adapter_version", dictionary_string(), nullable=False),
            pa.field("acquisition_cycle_id", pa.string(), nullable=False),
            pa.field("sport", dictionary_string(), nullable=False),
            pa.field("observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("event_count", pa.int32(), nullable=False),
            pa.field("warning_count", pa.int32(), nullable=False),
            pa.field("drift_code_count", pa.int32(), nullable=False),
            pa.field("provenance", pa.string(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_ACQUISITION_METADATA,
            schema_version=schema_version,
            domain=BOOKMAKERS_DOMAIN,
        ),
    )


def provider_status_schema(*, schema_version: str) -> pa.Schema:
    """Return the provider status dataset schema."""
    count_fields: list[pa.Field] = []
    if schema_version == BOOKMAKER_SCHEMA_VERSION_V2:
        count_fields = [
            pa.field("provider_native_markets", pa.int32(), nullable=False),
            pa.field("provider_native_priced_selections", pa.int32(), nullable=False),
            pa.field("canonical_markets", pa.int32(), nullable=False),
            pa.field("canonical_quotes", pa.int32(), nullable=False),
            pa.field("unmapped_markets", pa.int32(), nullable=False),
            pa.field("non_comparable_quotes", pa.int32(), nullable=False),
            pa.field("complete_events", pa.int32(), nullable=False),
            pa.field("partial_events", pa.int32(), nullable=False),
        ]
    return pa.schema(
        [
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("status_code", dictionary_string(), nullable=False),
            pa.field("last_attempted_acquisition_utc", utc_timestamp(), nullable=True),
            pa.field("last_successful_acquisition_utc", utc_timestamp(), nullable=True),
            pa.field("last_valid_snapshot_id", pa.string(), nullable=True),
            pa.field("snapshot_age_seconds", pa.int64(), nullable=True),
            pa.field("events_observed", pa.int32(), nullable=False),
            pa.field("valid_quotes_observed", pa.int32(), nullable=False),
            pa.field("unresolved_events", pa.int32(), nullable=False),
            pa.field("rejected_markets", pa.int32(), nullable=False),
            *count_fields,
            pa.field("warnings", pa.string(), nullable=False),
            pa.field(
                "current_block_or_failure_classification", dictionary_string(), nullable=False
            ),
            pa.field("next_eligible_attempt_utc", utc_timestamp(), nullable=True),
            pa.field("adapter_version", dictionary_string(), nullable=False),
            pa.field("observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PROVIDER_STATUS,
            schema_version=schema_version,
            domain=BOOKMAKERS_DOMAIN,
        ),
    )


def parser_drift_findings_schema(*, schema_version: str) -> pa.Schema:
    """Return the parser/drift findings dataset schema."""
    return pa.schema(
        [
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("code", dictionary_string(), nullable=False),
            pa.field("message", pa.string(), nullable=False),
            pa.field("severity", dictionary_string(), nullable=False),
            pa.field("source_path", pa.string(), nullable=True),
            pa.field("acquisition_cycle_id", pa.string(), nullable=False),
            pa.field("observed_at_utc", utc_timestamp(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_PARSER_DRIFT_FINDINGS,
            schema_version=schema_version,
            domain=BOOKMAKERS_DOMAIN,
        ),
    )


def comparison_eligibility_schema(*, schema_version: str) -> pa.Schema:
    """Return the comparison eligibility dataset schema."""
    return pa.schema(
        [
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("canonical_market_definition_id", dictionary_string(), nullable=False),
            pa.field("canonical_selection_id", dictionary_string(), nullable=False),
            pa.field("provider_id", dictionary_string(), nullable=False),
            pa.field("eligible", pa.bool_(), nullable=False),
            pa.field("reason", pa.string(), nullable=True),
            pa.field("quote_observation_id", pa.string(), nullable=False),
            pa.field("line_type", dictionary_string(), nullable=False),
            pa.field("line_value", pa.string(), nullable=True),
            pa.field("market_period", dictionary_string(), nullable=False),
            pa.field("participant_scope", dictionary_string(), nullable=False),
            pa.field("canonical_participant_id", pa.string(), nullable=True),
            pa.field("overtime_scope", dictionary_string(), nullable=True),
            pa.field("rules_scope", dictionary_string(), nullable=True),
            pa.field("comparable", pa.bool_(), nullable=False),
            pa.field("schema_version", dictionary_string(), nullable=False),
        ],
        metadata=dataset_metadata(
            dataset_name=DATASET_COMPARISON_ELIGIBILITY,
            schema_version=schema_version,
            domain=BOOKMAKERS_DOMAIN,
        ),
    )


def acquisition_metadata_rows(
    records: tuple[AcquisitionMetadataRecord, ...],
) -> list[dict[str, Any]]:
    """Build acquisition metadata rows in deterministic order."""
    return [
        {
            "provider_id": item.provider_id,
            "adapter_version": item.adapter_version,
            "acquisition_cycle_id": item.acquisition_cycle_id,
            "sport": item.sport,
            "observed_at_utc": item.observed_at_utc,
            "event_count": item.event_count,
            "warning_count": item.warning_count,
            "drift_code_count": item.drift_code_count,
            "provenance": "|".join(item.provenance),
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def provider_status_rows(records: tuple[ProviderStatusRecord, ...]) -> list[dict[str, Any]]:
    """Build provider status rows in deterministic order."""
    return [
        {
            "provider_id": item.provider_id,
            "status_code": item.status_code.value,
            "last_attempted_acquisition_utc": item.last_attempted_acquisition_utc,
            "last_successful_acquisition_utc": item.last_successful_acquisition_utc,
            "last_valid_snapshot_id": item.last_valid_snapshot_id,
            "snapshot_age_seconds": item.snapshot_age_seconds,
            "events_observed": item.events_observed,
            "valid_quotes_observed": item.valid_quotes_observed,
            "unresolved_events": item.unresolved_events,
            "rejected_markets": item.rejected_markets,
            "provider_native_markets": item.provider_native_markets,
            "provider_native_priced_selections": item.provider_native_priced_selections,
            "canonical_markets": item.canonical_markets,
            "canonical_quotes": item.canonical_quotes,
            "unmapped_markets": item.unmapped_markets,
            "non_comparable_quotes": item.non_comparable_quotes,
            "complete_events": item.complete_events,
            "partial_events": item.partial_events,
            "warnings": "|".join(item.warnings),
            "current_block_or_failure_classification": (
                item.current_block_or_failure_classification.value
            ),
            "next_eligible_attempt_utc": item.next_eligible_attempt_utc,
            "adapter_version": item.adapter_version,
            "observed_at_utc": item.observed_at_utc,
            "schema_version": BOOKMAKER_SCHEMA_VERSION,
        }
        for item in sorted(records, key=lambda row: row.provider_id)
    ]


def parser_drift_finding_rows(
    records: tuple[ParserDriftFinding, ...],
) -> list[dict[str, Any]]:
    """Build parser/drift finding rows in deterministic order."""
    return [
        {
            "provider_id": item.provider_id,
            "code": item.code,
            "message": item.message,
            "severity": item.severity,
            "source_path": item.source_path,
            "acquisition_cycle_id": item.acquisition_cycle_id,
            "observed_at_utc": item.observed_at_utc,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def comparison_eligibility_rows(
    records: tuple[ComparisonEligibilityRecord, ...],
) -> list[dict[str, Any]]:
    """Build comparison eligibility rows in deterministic order."""
    return [
        {
            "canonical_event_id": item.canonical_event_id,
            "canonical_market_definition_id": item.canonical_market_definition_id,
            "canonical_selection_id": item.canonical_selection_id,
            "provider_id": item.provider_id,
            "eligible": item.eligible,
            "reason": item.reason,
            "quote_observation_id": item.quote_observation_id,
            "line_type": item.line_type,
            "line_value": item.line_value,
            "market_period": item.market_period,
            "participant_scope": item.participant_scope,
            "canonical_participant_id": item.canonical_participant_id,
            "overtime_scope": item.overtime_scope,
            "rules_scope": item.rules_scope,
            "comparable": item.comparable,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _build_suite(*, sport_code: str, schema_version: str) -> SnapshotDatasetSuite:
    version = schema_version
    descriptors: tuple[DatasetDescriptor, ...] = (
        DatasetDescriptor(
            dataset_name=DATASET_ACQUISITION_METADATA,
            relative_filename="acquisition_metadata.parquet",
            schema=acquisition_metadata_schema(schema_version=version),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_PROVIDER_STATUS,
            relative_filename="provider_status.parquet",
            schema=provider_status_schema(schema_version=version),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_SOURCE_PARTICIPANTS,
            relative_filename="source_participants.parquet",
            schema=source_participants_schema(schema_version=version, sport_code=sport_code),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_PARTICIPANT_RECONCILIATIONS,
            relative_filename="participant_reconciliations.parquet",
            schema=participant_reconciliations_schema(
                schema_version=version,
                sport_code=sport_code,
            ),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_SOURCE_EVENTS,
            relative_filename="source_events.parquet",
            schema=source_events_schema(schema_version=version, sport_code=sport_code),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_EVENT_RECONCILIATIONS,
            relative_filename="event_reconciliations.parquet",
            schema=event_reconciliations_schema(schema_version=version, sport_code=sport_code),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_CANONICAL_EVENTS,
            relative_filename="canonical_events.parquet",
            schema=events_schema(schema_version=version, sport_code=sport_code),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_MARKET_QUOTES,
            relative_filename="market_quotes.parquet",
            schema=market_quotes_schema(schema_version=version),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_PARSER_DRIFT_FINDINGS,
            relative_filename="parser_drift_findings.parquet",
            schema=parser_drift_findings_schema(schema_version=version),
        ),
        DatasetDescriptor(
            dataset_name=DATASET_COMPARISON_ELIGIBILITY,
            relative_filename="comparison_eligibility.parquet",
            schema=comparison_eligibility_schema(schema_version=version),
        ),
    )
    primary_dataset_name = DATASET_MARKET_QUOTES
    if version == BOOKMAKER_SCHEMA_VERSION_V2:
        descriptors = (
            DatasetDescriptor(
                dataset_name=DATASET_PROVIDER_NATIVE_EVENTS,
                relative_filename="provider_native_events.parquet",
                schema=provider_native_events_schema(schema_version=version),
            ),
            DatasetDescriptor(
                dataset_name=DATASET_PROVIDER_NATIVE_MARKETS,
                relative_filename="provider_native_markets.parquet",
                schema=provider_native_markets_schema(schema_version=version),
            ),
            DatasetDescriptor(
                dataset_name=DATASET_PROVIDER_NATIVE_SELECTIONS,
                relative_filename="provider_native_selections.parquet",
                schema=provider_native_selections_schema(schema_version=version),
            ),
            *descriptors,
        )
        primary_dataset_name = DATASET_PROVIDER_NATIVE_SELECTIONS
    return SnapshotDatasetSuite(
        descriptors=descriptors,
        primary_dataset_name=primary_dataset_name,
    )


def bookmaker_snapshot_suite(
    *,
    sport_code: str,
    schema_version: str = BOOKMAKER_SCHEMA_VERSION,
) -> SnapshotDatasetSuite:
    """Return the immutable current-bookmaker-odds dataset suite for ``sport_code``."""
    return _build_suite(sport_code=sport_code, schema_version=schema_version)


def bundle_to_tables(
    bundle: NormalizedBookmakerBundle,
    *,
    sport_code: str,
    provider_statuses: tuple[ProviderStatusRecord, ...] = (),
    provider_bundle: ProviderAcquisitionBundle | None = None,
    schema_version: str = BOOKMAKER_SCHEMA_VERSION,
) -> dict[str, pa.Table]:
    """Convert a normalized bookmaker bundle into explicitly typed Arrow tables."""
    suite = bookmaker_snapshot_suite(
        sport_code=sport_code,
        schema_version=schema_version,
    )
    rows_by_dataset: dict[str, list[dict[str, Any]]] = {
        DATASET_ACQUISITION_METADATA: acquisition_metadata_rows(bundle.acquisition_metadata),
        DATASET_PROVIDER_STATUS: provider_status_rows(provider_statuses),
        DATASET_SOURCE_PARTICIPANTS: source_participant_rows(bundle.participants),
        DATASET_PARTICIPANT_RECONCILIATIONS: participant_reconciliation_rows(
            bundle.participant_reconciliations
        ),
        DATASET_SOURCE_EVENTS: source_event_rows(bundle.source_events),
        DATASET_EVENT_RECONCILIATIONS: event_reconciliation_rows(bundle.reconciliations),
        DATASET_CANONICAL_EVENTS: event_rows(tuple(item.canonical for item in bundle.events)),
        DATASET_MARKET_QUOTES: market_quote_rows(bundle.market_quotes),
        DATASET_PARSER_DRIFT_FINDINGS: parser_drift_finding_rows(bundle.parser_drift_findings),
        DATASET_COMPARISON_ELIGIBILITY: comparison_eligibility_rows(bundle.comparison_eligibility),
    }
    if provider_bundle is not None:
        if schema_version != BOOKMAKER_SCHEMA_VERSION_V2:
            msg = "provider-native inventory requires bookmaker-native-v2"
            raise ValueError(msg)
        rows_by_dataset.update(provider_native_rows(provider_bundle, schema_version=schema_version))
    tables: dict[str, pa.Table] = {}
    for descriptor in suite.descriptors:
        rows = rows_by_dataset[descriptor.dataset_name]
        tables[descriptor.dataset_name] = pa.Table.from_pylist(rows, schema=descriptor.schema)
    return tables

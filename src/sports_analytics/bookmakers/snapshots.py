"""Snapshot specification and publication helpers for current bookmaker odds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sports_analytics.bookmakers.normalization import NormalizedBookmakerBundle
from sports_analytics.bookmakers.schemas import bookmaker_snapshot_suite, bundle_to_tables
from sports_analytics.bookmakers.status import ProviderStatusRecord
from sports_analytics.bookmakers.types import (
    BOOKMAKER_COMBINED_SOURCE_NAME,
    BOOKMAKER_NORMALIZER_VERSION,
    BOOKMAKER_SCHEMA_VERSION,
    BOOKMAKER_SNAPSHOT_TYPE,
    PARTITION_KEY_SPORT,
)
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import JsonValue, validate_identifier, validate_sha256_checksum
from sports_analytics.snapshots.service import SnapshotPublicationService
from sports_analytics.snapshots.spec import (
    RawArtifactReference,
    SnapshotHttpMetadata,
    SnapshotIdentity,
    SnapshotSpec,
)
from sports_analytics.snapshots.types import PublishedSnapshot
from sports_analytics.snapshots.writer import (
    PreparedSnapshot,
    discard_prepared_snapshot,
    prepare_snapshot_directory,
)
from sports_analytics.sports.contracts import require_utc

BOOKMAKER_SOURCE_POLICY_VERSION: Final[str] = "bookmaker-source-policy-v1"


@dataclass(frozen=True, slots=True)
class BookmakerSnapshotPublicationResult:
    """Typed summary of a published current-bookmaker-odds snapshot."""

    published: PublishedSnapshot
    sport_code: str
    quote_count: int
    event_count: int


def build_bookmaker_snapshot_spec(
    *,
    sport_code: str,
    source_version: str,
    source_observed_at_utc: datetime,
    bundle: NormalizedBookmakerBundle,
    raw_artifact: RawArtifactReference | None = None,
    producer_versions: dict[str, str] | None = None,
    domain_metadata: dict[str, JsonValue] | None = None,
) -> SnapshotSpec:
    """Build the validated snapshot specification for one bookmaker normalize pass."""
    validate_identifier(sport_code, field_name="sport_code")
    validate_identifier(source_version, field_name="source_version")
    observed = require_utc(source_observed_at_utc, field_name="source_observed_at_utc")
    identity = SnapshotIdentity(
        snapshot_type=BOOKMAKER_SNAPSHOT_TYPE,
        schema_version=BOOKMAKER_SCHEMA_VERSION,
        source_name=BOOKMAKER_COMBINED_SOURCE_NAME,
        source_version=source_version,
        partition_keys=((PARTITION_KEY_SPORT, sport_code),),
    )
    artifact = raw_artifact or RawArtifactReference(
        relative_path="raw/bookmakers/empty.txt",
        checksum_sha256="0" * 64,
        byte_count=0,
        encoding="utf-8",
    )
    producers = {
        "normalizer": BOOKMAKER_NORMALIZER_VERSION,
        "event_reconciliation": bundle.reconciliation_policy_version,
        "participant_reconciliation": bundle.participant_reconciliation_policy_version,
    }
    if producer_versions:
        producers.update(producer_versions)
    metadata: dict[str, JsonValue] = {
        "sport_code": sport_code,
        "schema_version": BOOKMAKER_SCHEMA_VERSION,
        "unknown_market_count": len(bundle.unknown_markets),
        "quote_count": len(bundle.market_quotes),
        "resolved_event_count": len(bundle.events),
        "source_event_count": len(bundle.source_events),
    }
    if domain_metadata:
        metadata.update(domain_metadata)
    quality_summary = {
        "warnings_count": len(bundle.warnings),
        "parser_drift_findings_count": len(bundle.parser_drift_findings),
        "comparison_eligibility_count": len(bundle.comparison_eligibility),
        "unknown_market_count": len(bundle.unknown_markets),
    }
    return SnapshotSpec(
        identity=identity,
        suite=bookmaker_snapshot_suite(sport_code=sport_code),
        source_url="bookmakers://current-odds",
        source_policy_version=BOOKMAKER_SOURCE_POLICY_VERSION,
        source_observed_at_utc=observed,
        raw_artifact=artifact,
        http_metadata=SnapshotHttpMetadata(network_retrieved=False),
        producer_versions=producers,
        domain_metadata=metadata,
        quality_summary=quality_summary,
        warnings=bundle.warnings,
    )


def prepare_bookmaker_snapshot(
    *,
    snapshots_directory: Path,
    sport_code: str,
    source_version: str,
    source_observed_at_utc: datetime,
    bundle: NormalizedBookmakerBundle,
    provider_statuses: tuple[ProviderStatusRecord, ...] = (),
    raw_artifact: RawArtifactReference | None = None,
    producer_versions: dict[str, str] | None = None,
    domain_metadata: dict[str, JsonValue] | None = None,
) -> PreparedSnapshot:
    """Prepare an immutable current-bookmaker-odds snapshot directory."""
    spec = build_bookmaker_snapshot_spec(
        sport_code=sport_code,
        source_version=source_version,
        source_observed_at_utc=source_observed_at_utc,
        bundle=bundle,
        raw_artifact=raw_artifact,
        producer_versions=producer_versions,
        domain_metadata=domain_metadata,
    )
    tables = bundle_to_tables(
        bundle,
        sport_code=sport_code,
        provider_statuses=provider_statuses,
    )
    return prepare_snapshot_directory(
        snapshots_directory=snapshots_directory,
        spec=spec,
        tables=tables,
    )


def publish_bookmaker_snapshot(
    *,
    database_path: Path,
    snapshots_directory: Path,
    sport_code: str,
    source_version: str,
    source_observed_at_utc: datetime,
    bundle: NormalizedBookmakerBundle,
    provider_statuses: tuple[ProviderStatusRecord, ...] = (),
    raw_artifact: RawArtifactReference | None = None,
    producer_versions: dict[str, str] | None = None,
    domain_metadata: dict[str, JsonValue] | None = None,
    actor: str = "bookmaker-snapshot-service",
    correlation_id: str | None = None,
) -> BookmakerSnapshotPublicationResult:
    """Prepare and publish a READY current-bookmaker-odds snapshot via SQLite."""
    prepared: PreparedSnapshot | None = None
    owns_prepared = False
    try:
        prepared = prepare_bookmaker_snapshot(
            snapshots_directory=snapshots_directory,
            sport_code=sport_code,
            source_version=source_version,
            source_observed_at_utc=source_observed_at_utc,
            bundle=bundle,
            provider_statuses=provider_statuses,
            raw_artifact=raw_artifact,
            producer_versions=producer_versions,
            domain_metadata=domain_metadata,
        )
        owns_prepared = True
        publisher = SnapshotPublicationService(
            database_path=database_path,
            snapshots_directory=snapshots_directory,
            suite=bookmaker_snapshot_suite(sport_code=sport_code),
        )
        published = publisher.publish_or_reuse(
            prepared,
            actor=actor,
            correlation_id=correlation_id,
        )
        owns_prepared = False
        return BookmakerSnapshotPublicationResult(
            published=published,
            sport_code=sport_code,
            quote_count=len(bundle.market_quotes),
            event_count=len(bundle.events),
        )
    except Exception as exc:
        if owns_prepared and prepared is not None:
            try:
                discard_prepared_snapshot(prepared)
            except Exception:
                pass
            owns_prepared = False
        raise PermanentSourceError(f"bookmaker snapshot publication failed: {exc}") from exc


def build_bookmaker_source_version(
    *,
    sport_code: str,
    acquisition_cycle_id: str,
    raw_sha256: str,
) -> str:
    """Build a deterministic source_version for bookmaker snapshot deduplication."""
    checksum = validate_sha256_checksum(raw_sha256)
    value = f"{sport_code}:{acquisition_cycle_id}:sha256:{checksum}"
    return validate_identifier(value, field_name="source_version")

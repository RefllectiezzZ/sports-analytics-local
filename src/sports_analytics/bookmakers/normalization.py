"""Normalize provider acquisition bundles into canonical current odds quotes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sports_analytics.bookmakers.canonical_mapping import quote_is_comparable
from sports_analytics.bookmakers.markets import (
    UnknownProviderMarket,
    canonical_selection,
    map_provider_market_to_canonical,
)
from sports_analytics.bookmakers.reconciliation import (
    BOOKMAKER_SEASON_LABEL,
    BookmakerReconciliationBundle,
    competition_id_for_event,
    home_away_participants,
    normalize_participant_name,
    occurrence_key_for_event,
    participant_identity_scope_for_sport,
    participant_type_for_sport,
    sanitize_competition_events,
)
from sports_analytics.bookmakers.types import (
    BOOKMAKER_NORMALIZER_VERSION,
    BOOKMAKER_SCHEMA_VERSION,
)
from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.markets.contracts import (
    MarketStatus,
    OddsQuote,
    ProviderType,
    QuotePhase,
    QuoteQualityStatus,
    QuoteTimestampPrecision,
    SelectionStatus,
)
from sports_analytics.markets.identifiers import (
    build_quote_observation_id,
    build_quote_series_id,
)
from sports_analytics.markets.schemas import quote_sort_key
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderParserWarning,
    ProviderSelectionPriceState,
)
from sports_analytics.sports.contracts import (
    CanonicalParticipant,
    EventReconciliation,
    EventStatus,
    IngestedEvent,
    IngestedParticipant,
    IngestedSourceEvent,
    OutcomeAvailability,
    ParticipantReconciliation,
    SourceEventReference,
    SourceParticipantReference,
    StartTimePrecision,
)
from sports_analytics.sports.event_metadata import resolve_canonical_events_from_sources
from sports_analytics.sports.identifiers import (
    build_season_id,
    build_source_event_key,
    build_source_participant_key,
)
from sports_analytics.sports.reconciliation import PARTICIPANT_RECONCILIATION_POLICY_VERSION

EMPTY_SOURCE_FILE_SHA256: Final[str] = "0" * 64


@dataclass(frozen=True, slots=True)
class ParserDriftFinding:
    """One auditable parser/drift finding retained in the bookmaker snapshot."""

    provider_id: str
    code: str
    message: str
    severity: str
    source_path: str | None
    acquisition_cycle_id: str
    observed_at_utc: datetime
    schema_version: str = BOOKMAKER_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ComparisonEligibilityRecord:
    """Whether a resolved canonical selection is eligible for cross-bookmaker compare."""

    canonical_event_id: str
    canonical_market_definition_id: str
    canonical_selection_id: str
    provider_id: str
    eligible: bool
    reason: str | None
    quote_observation_id: str = ""
    line_type: str = "none"
    line_value: str | None = None
    market_period: str = "full-match"
    participant_scope: str = "event"
    canonical_participant_id: str | None = None
    overtime_scope: str | None = None
    rules_scope: str | None = None
    comparable: bool = False
    schema_version: str = BOOKMAKER_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AcquisitionMetadataRecord:
    """One provider acquisition cycle summarized for the snapshot."""

    provider_id: str
    adapter_version: str
    acquisition_cycle_id: str
    sport: str
    observed_at_utc: datetime
    event_count: int
    warning_count: int
    drift_code_count: int
    provenance: tuple[str, ...]
    schema_version: str = BOOKMAKER_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class NormalizedBookmakerBundle:
    """Canonical and source-scoped bookmaker datasets from one normalize pass."""

    acquisition_metadata: tuple[AcquisitionMetadataRecord, ...]
    participants: tuple[IngestedParticipant, ...]
    source_events: tuple[IngestedSourceEvent, ...]
    events: tuple[IngestedEvent, ...]
    market_quotes: tuple[OddsQuote, ...]
    parser_drift_findings: tuple[ParserDriftFinding, ...]
    comparison_eligibility: tuple[ComparisonEligibilityRecord, ...]
    unknown_markets: tuple[UnknownProviderMarket, ...]
    warnings: tuple[str, ...]
    normalizer_version: str
    reconciliation_policy_version: str
    participant_reconciliation_policy_version: str
    schema_version: str

    @property
    def reconciliations(self) -> tuple[EventReconciliation, ...]:
        """Return every source-event reconciliation decision."""
        return tuple(
            sorted(
                (event.reconciliation for event in self.source_events),
                key=lambda item: (item.source_name, item.source_event_id),
            )
        )

    @property
    def participant_reconciliations(self) -> tuple[ParticipantReconciliation, ...]:
        """Return every source-participant reconciliation decision."""
        return tuple(
            sorted(
                (participant.reconciliation for participant in self.participants),
                key=lambda item: (item.source_name, item.source_participant_id),
            )
        )


def normalize_bookmaker_bundles(
    bundles: tuple[ProviderAcquisitionBundle, ...],
    *,
    reconciliations: BookmakerReconciliationBundle,
    source_file_sha256: str = EMPTY_SOURCE_FILE_SHA256,
) -> NormalizedBookmakerBundle:
    """Convert provider bundles into CURRENT OddsQuote records for resolved events."""
    if not bundles:
        msg = "at least one provider acquisition bundle is required"
        raise NormalizationError(msg)
    bundles = sanitize_competition_events(bundles)

    sports = {bundle.sport for bundle in bundles}
    if len(sports) != 1:
        msg = "all provider bundles in one normalize pass must share one sport"
        raise NormalizationError(msg)

    participant_by_source = {
        (item.source_name, item.source_participant_id): item
        for item in reconciliations.participant_reconciliations
    }
    event_by_source = {
        (item.source_name, item.source_event_id): item
        for item in reconciliations.event_reconciliations
    }

    participants = _build_participants(
        bundles=bundles,
        participant_by_source=participant_by_source,
    )
    source_events = _build_source_events(
        bundles=bundles,
        event_by_source=event_by_source,
        participant_by_source=participant_by_source,
        source_file_sha256=source_file_sha256,
    )
    events = resolve_canonical_events_from_sources(source_events)

    quotes, unknown_markets, eligibility = _build_quotes(
        bundles=bundles,
        event_by_source=event_by_source,
        source_file_sha256=source_file_sha256,
    )
    drift = _build_drift_findings(bundles)
    metadata = tuple(
        AcquisitionMetadataRecord(
            provider_id=bundle.provider_id,
            adapter_version=bundle.adapter_version,
            acquisition_cycle_id=bundle.acquisition_cycle_id,
            sport=bundle.sport,
            observed_at_utc=bundle.observed_at_utc,
            event_count=len(bundle.events),
            warning_count=len(bundle.warnings),
            drift_code_count=len(bundle.drift_codes),
            provenance=bundle.provenance,
        )
        for bundle in sorted(bundles, key=lambda item: item.provider_id)
    )
    warnings = tuple(
        sorted(
            {
                f"{bundle.provider_id}:{warning.code}:{warning.message}"
                for bundle in bundles
                for warning in bundle.warnings
            }
        )
    )
    return NormalizedBookmakerBundle(
        acquisition_metadata=metadata,
        participants=participants,
        source_events=source_events,
        events=events,
        market_quotes=tuple(sorted(quotes, key=quote_sort_key)),
        parser_drift_findings=drift,
        comparison_eligibility=tuple(
            sorted(
                eligibility,
                key=lambda item: (
                    item.canonical_event_id,
                    item.canonical_market_definition_id,
                    item.canonical_selection_id,
                    item.provider_id,
                ),
            )
        ),
        unknown_markets=unknown_markets,
        warnings=warnings,
        normalizer_version=BOOKMAKER_NORMALIZER_VERSION,
        reconciliation_policy_version=reconciliations.policy_version,
        participant_reconciliation_policy_version=PARTICIPANT_RECONCILIATION_POLICY_VERSION,
        schema_version=BOOKMAKER_SCHEMA_VERSION,
    )


def _build_participants(
    *,
    bundles: tuple[ProviderAcquisitionBundle, ...],
    participant_by_source: dict[tuple[str, str], ParticipantReconciliation],
) -> tuple[IngestedParticipant, ...]:
    ingested: list[IngestedParticipant] = []
    seen: set[tuple[str, str]] = set()
    for bundle in bundles:
        sport = bundle.sport
        participant_type = participant_type_for_sport(sport)
        identity_scope = participant_identity_scope_for_sport(sport)
        for event in bundle.events:
            competition_id = competition_id_for_event(event, provider_id=bundle.provider_id)
            for participant in event.participants:
                identity = (bundle.provider_id, participant.source_participant_id)
                if identity in seen:
                    continue
                seen.add(identity)
                recon = participant_by_source.get(identity)
                if recon is None:
                    msg = (
                        "missing participant reconciliation for "
                        f"{bundle.provider_id}/{participant.source_participant_id}"
                    )
                    raise NormalizationError(msg)
                display_name, normalized_name = normalize_participant_name(
                    participant.normalized_name or participant.display_name
                )
                source_participant_key = build_source_participant_key(
                    source_name=bundle.provider_id,
                    sport_code=sport,
                    competition_id=competition_id,
                    normalized_name=normalized_name,
                )
                canonical: CanonicalParticipant | None = None
                canonical_id = recon.canonical_participant_id
                if recon.is_downstream_safe and canonical_id is not None:
                    canonical = CanonicalParticipant(
                        canonical_participant_id=canonical_id,
                        sport_code=sport,
                        participant_identity_scope=identity_scope,
                        participant_type=participant_type,
                        canonical_key=normalized_name,
                        display_name=display_name,
                        schema_version=BOOKMAKER_SCHEMA_VERSION,
                    )
                source_reference = SourceParticipantReference(
                    source_participant_id=participant.source_participant_id,
                    source_name=bundle.provider_id,
                    source_participant_key=source_participant_key,
                    canonical_participant_id=canonical_id if recon.is_downstream_safe else None,
                    competition_id=competition_id,
                    participant_type=participant_type,
                    display_name=display_name,
                    normalized_name=normalized_name,
                    schema_version=BOOKMAKER_SCHEMA_VERSION,
                )
                ingested.append(
                    IngestedParticipant(
                        source_reference=source_reference,
                        reconciliation=recon,
                        canonical=canonical,
                    )
                )
    return tuple(
        sorted(
            ingested,
            key=lambda item: (
                item.source_reference.source_name,
                item.source_reference.source_participant_id,
            ),
        )
    )


def _build_source_events(
    *,
    bundles: tuple[ProviderAcquisitionBundle, ...],
    event_by_source: dict[tuple[str, str], EventReconciliation],
    participant_by_source: dict[tuple[str, str], ParticipantReconciliation],
    source_file_sha256: str,
) -> tuple[IngestedSourceEvent, ...]:
    ingested: list[IngestedSourceEvent] = []
    row_number = 0
    for bundle in sorted(bundles, key=lambda item: item.provider_id):
        for event in bundle.events:
            row_number += 1
            recon = event_by_source.get((bundle.provider_id, event.source_event_id))
            if recon is None:
                msg = (
                    f"missing event reconciliation for {bundle.provider_id}/{event.source_event_id}"
                )
                raise NormalizationError(msg)
            home, away = home_away_participants(event.participants)
            competition_id = competition_id_for_event(event, provider_id=bundle.provider_id)
            season_id = build_season_id(
                competition_id=competition_id,
                label=BOOKMAKER_SEASON_LABEL,
            )
            home_recon = participant_by_source.get((bundle.provider_id, home.source_participant_id))
            away_recon = participant_by_source.get((bundle.provider_id, away.source_participant_id))
            home_canonical = (
                home_recon.canonical_participant_id
                if home_recon is not None and home_recon.is_downstream_safe
                else None
            )
            away_canonical = (
                away_recon.canonical_participant_id
                if away_recon is not None and away_recon.is_downstream_safe
                else None
            )
            _, home_normalized = normalize_participant_name(
                home.normalized_name or home.display_name
            )
            _, away_normalized = normalize_participant_name(
                away.normalized_name or away.display_name
            )
            home_source_key = build_source_participant_key(
                source_name=bundle.provider_id,
                sport_code=bundle.sport,
                competition_id=competition_id,
                normalized_name=home_normalized,
            )
            away_source_key = build_source_participant_key(
                source_name=bundle.provider_id,
                sport_code=bundle.sport,
                competition_id=competition_id,
                normalized_name=away_normalized,
            )
            source_event_key = build_source_event_key(
                source_name=bundle.provider_id,
                competition_id=competition_id,
                season_id=season_id,
                event_date=event.scheduled_start_utc.date(),
                home_source_participant_key=home_source_key,
                away_source_participant_key=away_source_key,
            )
            canonical_event_id = recon.canonical_event_id if recon.is_downstream_safe else None
            source_reference = SourceEventReference(
                source_event_id=event.source_event_id,
                source_name=bundle.provider_id,
                source_event_key=source_event_key,
                canonical_event_id=canonical_event_id,
                source_row_number=row_number,
                source_observed_at_utc=bundle.observed_at_utc,
                source_file_sha256=source_file_sha256,
                schema_version=BOOKMAKER_SCHEMA_VERSION,
            )
            ingested.append(
                IngestedSourceEvent(
                    source_reference=source_reference,
                    reconciliation=recon,
                    sport_code=bundle.sport,
                    competition_id=competition_id,
                    season_id=season_id,
                    event_occurrence_key=occurrence_key_for_event(
                        scheduled_start_utc=event.scheduled_start_utc
                    ),
                    event_date=event.scheduled_start_utc.date(),
                    scheduled_start_utc=event.scheduled_start_utc,
                    start_time_precision=StartTimePrecision.MINUTE.value,
                    status=EventStatus.SCHEDULED.value,
                    home_source_participant_id=home.source_participant_id,
                    away_source_participant_id=away.source_participant_id,
                    home_canonical_participant_id=home_canonical,
                    away_canonical_participant_id=away_canonical,
                    home_score=None,
                    away_score=None,
                    result_code=None,
                    outcome_availability_stage=OutcomeAvailability.PRE_EVENT_UNAVAILABLE.value,
                    schema_version=BOOKMAKER_SCHEMA_VERSION,
                )
            )
    return tuple(
        sorted(
            ingested,
            key=lambda item: (
                item.source_reference.source_name,
                item.source_reference.source_event_id,
            ),
        )
    )


def _build_quotes(
    *,
    bundles: tuple[ProviderAcquisitionBundle, ...],
    event_by_source: dict[tuple[str, str], EventReconciliation],
    source_file_sha256: str,
) -> tuple[
    list[OddsQuote],
    tuple[UnknownProviderMarket, ...],
    list[ComparisonEligibilityRecord],
]:
    quotes: list[OddsQuote] = []
    unknown: list[UnknownProviderMarket] = []
    eligibility: list[ComparisonEligibilityRecord] = []
    for bundle in bundles:
        for event in bundle.events:
            recon = event_by_source[(bundle.provider_id, event.source_event_id)]
            if not recon.is_downstream_safe or recon.canonical_event_id is None:
                # Unresolved events remain auditable but never enter canonical quotes.
                continue
            for market in event.markets:
                if market.market_status is not MarketStatus.OPEN:
                    # Suspended/closed markets remain provider-auditable but never
                    # enter the current-odds quote catalogue.
                    continue
                mapped = map_provider_market_to_canonical(market)
                if isinstance(mapped, UnknownProviderMarket):
                    unknown.append(mapped)
                    continue
                for selection_obs in market.selections:
                    if (
                        selection_obs.canonical_outcome_key is None
                        or selection_obs.price_state is not ProviderSelectionPriceState.PRICED
                        or selection_obs.decimal_odds is None
                        or selection_obs.selection_status is not SelectionStatus.ACTIVE
                    ):
                        continue
                    outcome_key = selection_obs.canonical_outcome_key.value
                    quote_source_file_sha256 = selection_obs.source_capture_id or source_file_sha256
                    selection = canonical_selection(
                        mapped.definition,
                        outcome_key=outcome_key,
                        source_market_id=market.source_market_id,
                        source_selection_id=selection_obs.source_selection_id,
                    )
                    series_id = build_quote_series_id(
                        canonical_event_id=recon.canonical_event_id,
                        selection=selection,
                        provider_type=ProviderType.BOOKMAKER.value,
                        provider_id=bundle.provider_id,
                    )
                    observation_id = build_quote_observation_id(
                        quote_series_id=series_id,
                        source_name=bundle.provider_id,
                        source_event_id=event.source_event_id,
                        selection=selection,
                        provider_type=ProviderType.BOOKMAKER.value,
                        provider_id=bundle.provider_id,
                        quote_phase=QuotePhase.CURRENT.value,
                        source_observed_at_utc=bundle.observed_at_utc,
                        quoted_at_utc=None,
                        source_file_sha256=quote_source_file_sha256,
                        source_field=None,
                    )
                    quotes.append(
                        OddsQuote(
                            quote_series_id=series_id,
                            quote_observation_id=observation_id,
                            canonical_event_id=recon.canonical_event_id,
                            source_name=bundle.provider_id,
                            source_event_id=event.source_event_id,
                            selection=selection,
                            provider_type=ProviderType.BOOKMAKER.value,
                            provider_id=bundle.provider_id,
                            decimal_odds=selection_obs.decimal_odds,
                            quote_phase=QuotePhase.CURRENT.value,
                            source_observed_at_utc=bundle.observed_at_utc,
                            quoted_at_utc=None,
                            quote_timestamp_precision=(
                                QuoteTimestampPrecision.SNAPSHOT_OBSERVATION_ONLY.value
                            ),
                            quote_valid_from_utc=None,
                            quote_valid_to_utc=None,
                            market_status=market.market_status.value,
                            selection_status=selection_obs.selection_status.value,
                            source_field=None,
                            quality_status=QuoteQualityStatus.SOURCE_PROVIDED.value,
                            quality_reason=None,
                            source_file_sha256=quote_source_file_sha256,
                            schema_version=BOOKMAKER_SCHEMA_VERSION,
                        )
                    )
                    line_value = (
                        None
                        if mapped.definition.line_value is None
                        else format(mapped.definition.line_value, "f")
                    )
                    overtime_scope = market.overtime_scope
                    rules_scope = market.rules_scope
                    comparable = quote_is_comparable(
                        definition_id=mapped.definition_id,
                        overtime_scope=overtime_scope,
                        rules_scope=rules_scope,
                    )
                    eligibility.append(
                        ComparisonEligibilityRecord(
                            canonical_event_id=recon.canonical_event_id,
                            canonical_market_definition_id=mapped.definition_id,
                            canonical_selection_id=outcome_key,
                            provider_id=bundle.provider_id,
                            eligible=comparable,
                            reason=None if comparable else "unknown-rules-or-overtime-scope",
                            quote_observation_id=observation_id,
                            line_type=mapped.definition.line_type,
                            line_value=line_value,
                            market_period=mapped.definition.market_period,
                            participant_scope=mapped.definition.participant_scope,
                            canonical_participant_id=mapped.definition.canonical_participant_id,
                            overtime_scope=overtime_scope,
                            rules_scope=rules_scope,
                            comparable=comparable,
                        )
                    )
    return quotes, tuple(unknown), eligibility


def _build_drift_findings(
    bundles: tuple[ProviderAcquisitionBundle, ...],
) -> tuple[ParserDriftFinding, ...]:
    findings: list[ParserDriftFinding] = []
    for bundle in bundles:
        for warning in bundle.warnings:
            findings.append(_warning_to_finding(bundle, warning))
        for code in bundle.drift_codes:
            findings.append(
                ParserDriftFinding(
                    provider_id=bundle.provider_id,
                    code=code,
                    message=f"parser drift code: {code}",
                    severity="warning",
                    source_path=None,
                    acquisition_cycle_id=bundle.acquisition_cycle_id,
                    observed_at_utc=bundle.observed_at_utc,
                )
            )
    return tuple(
        sorted(
            findings,
            key=lambda item: (item.provider_id, item.code, item.message, item.acquisition_cycle_id),
        )
    )


def _warning_to_finding(
    bundle: ProviderAcquisitionBundle,
    warning: ProviderParserWarning,
) -> ParserDriftFinding:
    return ParserDriftFinding(
        provider_id=bundle.provider_id,
        code=warning.code,
        message=warning.message,
        severity=warning.severity.value,
        source_path=warning.source_path,
        acquisition_cycle_id=bundle.acquisition_cycle_id,
        observed_at_utc=bundle.observed_at_utc,
    )

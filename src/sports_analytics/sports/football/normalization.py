"""Normalize Football-Data.co.uk rows into football-canonical-v2 records."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from sports_analytics.core.exceptions import NormalizationError, SourceIntegrityError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.markets.contracts import (
    MarketStatus,
    OddsQuote,
    QuoteQualityStatus,
    QuoteTimestampPrecision,
    SelectionStatus,
)
from sports_analytics.markets.identifiers import (
    build_quote_observation_id,
    build_quote_series_id,
)
from sports_analytics.markets.schemas import quote_sort_key
from sports_analytics.sports.contracts import (
    CanonicalEvent,
    CanonicalParticipant,
    CompetitionRecord,
    EventReconciliation,
    EventStatus,
    IngestedEvent,
    IngestedParticipant,
    IngestedSourceEvent,
    OutcomeAvailability,
    ParticipantReconciliation,
    ParticipantType,
    ReconciliationState,
    SeasonRecord,
    SourceEventReference,
    SourceParticipantReference,
    StartTimePrecision,
)
from sports_analytics.sports.football.contracts import FOOTBALL_CANONICAL_SCHEMA_VERSION
from sports_analytics.sports.football.markets import (
    MATCH_RESULT_1X2_OUTCOMES,
    SUPPORTED_ODDS_FAMILIES,
    OddsColumnFamily,
    match_result_1x2_selection,
)
from sports_analytics.sports.football.validation import (
    MAX_CARDS,
    MAX_CORNERS,
    MAX_FOULS,
    MAX_GOALS,
    MAX_REFEREE_LENGTH,
    MAX_SHOTS,
    MAX_TEAM_NAME_LENGTH,
    expected_result_from_goals,
    map_result_code,
    parse_decimal_odds,
    parse_optional_int,
    parse_required_pair,
)
from sports_analytics.sports.identifiers import (
    FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    SPORT_FOOTBALL,
    build_canonical_event_id,
    build_canonical_participant_id,
    build_season_id,
    build_source_event_id,
    build_source_event_key,
    build_source_participant_id,
    build_source_participant_key,
)
from sports_analytics.sports.reconciliation import (
    PARTICIPANT_RECONCILIATION_POLICY_VERSION,
    RECONCILIATION_POLICY_VERSION,
    ParticipantReconciliationCandidate,
    ReconciliationCandidate,
    reconcile_candidates,
    reconcile_participant_candidates,
)
from sports_analytics.sports.types import CompetitionType

PINNACLE_CAUTION_CUTOFF: Final[date] = date(2025, 7, 23)
SOURCE_QUALITY_POLICY_VERSION: Final[str] = "football-data-co-uk-policy-v1"
AVAILABILITY_POST_MATCH: Final[str] = "post-match"


@dataclass(frozen=True, slots=True)
class PostMatchStatisticsRecord:
    """Football statistics that only exist after an event has been played."""

    canonical_event_id: str
    source_event_id: str
    availability_stage: str
    half_time_home_goals: int | None
    half_time_away_goals: int | None
    half_time_result: str | None
    referee: str | None
    home_shots: int | None
    away_shots: int | None
    home_shots_on_target: int | None
    away_shots_on_target: int | None
    home_corners: int | None
    away_corners: int | None
    home_fouls: int | None
    away_fouls: int | None
    home_yellow_cards: int | None
    away_yellow_cards: int | None
    home_red_cards: int | None
    away_red_cards: int | None
    source_observed_at_utc: datetime
    source_file_sha256: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class NormalizedFootballBundle:
    """Canonical and source-scoped football datasets produced by one ingestion."""

    competitions: tuple[CompetitionRecord, ...]
    seasons: tuple[SeasonRecord, ...]
    participants: tuple[IngestedParticipant, ...]
    source_events: tuple[IngestedSourceEvent, ...]
    events: tuple[IngestedEvent, ...]
    unresolved_reconciliations: tuple[EventReconciliation, ...]
    market_quotes: tuple[OddsQuote, ...]
    post_match_statistics: tuple[PostMatchStatisticsRecord, ...]
    duplicate_rows_discarded: int
    warnings: tuple[str, ...]
    pinnacle_caution_quote_count: int
    source_policy_version: str
    reconciliation_policy_version: str
    participant_reconciliation_policy_version: str

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

    @property
    def unresolved_event_count(self) -> int:
        """Return the number of retained source events that did not reconcile."""
        return sum(
            1
            for event in self.source_events
            if event.reconciliation.state == ReconciliationState.UNRESOLVED.value
        )


@dataclass(frozen=True, slots=True)
class _ParsedSourceEvent:
    row: dict[str, str]
    row_number: int
    source_reference: SourceEventReference
    competition_id: str
    season_id: str
    event_occurrence_key: str
    event_date: date
    scheduled_start_utc: datetime | None
    start_time_precision: str
    status: str
    home_source_participant_id: str
    away_source_participant_id: str
    home_score: int | None
    away_score: int | None
    result_code: str | None
    outcome_availability_stage: str
    half_time_home_goals: int | None
    half_time_away_goals: int | None
    half_time_result: str | None


def normalize_team_name(value: str) -> tuple[str, str]:
    """Return ``(display_name, normalized_key)`` for a source team name."""
    if not isinstance(value, str):
        msg = "team name must be a string"
        raise NormalizationError(msg)
    if "\x00" in value:
        msg = "team name must not contain NUL"
        raise NormalizationError(msg)
    if any(unicodedata.category(ch)[0] == "C" and ch not in {"\t"} for ch in value):
        # Reject control characters other than tab (which is collapsed as whitespace).
        msg = "team name must not contain control characters"
        raise NormalizationError(msg)
    decoded = unicodedata.normalize("NFC", value)
    collapsed = " ".join(decoded.replace("\t", " ").split())
    if not collapsed:
        msg = "team name must be non-empty after normalization"
        raise NormalizationError(msg)
    if len(collapsed) > MAX_TEAM_NAME_LENGTH:
        msg = f"team name exceeds maximum length of {MAX_TEAM_NAME_LENGTH}"
        raise NormalizationError(msg)
    return collapsed, collapsed.casefold()


def parse_source_date(value: str) -> date:
    """Parse Football-Data date fields without pandas inference."""
    text = value.strip()
    if not text:
        msg = "Date is required"
        raise NormalizationError(msg)
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    msg = "Date must use DD/MM/YYYY or DD/MM/YY"
    raise NormalizationError(msg)


def parse_source_time(value: str) -> time | None:
    """Parse optional HH:MM 24-hour kickoff time."""
    text = value.strip()
    if text == "":
        return None
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        msg = "Time must use HH:MM 24-hour format"
        raise NormalizationError(msg) from exc
    return parsed.time().replace(second=0, microsecond=0)


def combine_local_kickoff(
    event_date: date,
    kickoff: time,
    *,
    timezone_name: str,
) -> datetime:
    """Combine local competition date/time into UTC with a deterministic DST policy.

    Ambiguous local times (DST fall-back) use the earlier occurrence (``fold=0``).
    Nonexistent local times (DST spring-forward gaps) are rejected.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001 - ZoneInfo raises varied errors
        msg = f"invalid competition timezone: {timezone_name}"
        raise NormalizationError(msg) from exc
    local = datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        kickoff.hour,
        kickoff.minute,
        tzinfo=zone,
        fold=0,
    )
    # Detect nonexistent times by round-tripping through UTC.
    reconstituted = local.astimezone(UTC).astimezone(zone)
    if (
        reconstituted.year != local.year
        or reconstituted.month != local.month
        or reconstituted.day != local.day
        or reconstituted.hour != local.hour
        or reconstituted.minute != local.minute
    ):
        msg = "Time does not exist in the competition local timezone"
        raise NormalizationError(msg)
    return local.astimezone(UTC)


def normalize_football_rows(
    *,
    rows: list[dict[str, str]],
    competition_id: str,
    competition_display_name: str,
    country_code: str,
    source_competition_code: str,
    timezone_name: str,
    season_label: str,
    start_year: int,
    end_year: int,
    source_season_code: str,
    source_name: str,
    source_file_sha256: str,
    source_observed_at_utc: datetime,
) -> NormalizedFootballBundle:
    """Normalize parsed CSV rows into canonical and source-scoped datasets."""
    if source_observed_at_utc.tzinfo is None:
        msg = "source_observed_at_utc must be timezone-aware"
        raise NormalizationError(msg)
    observed = source_observed_at_utc.astimezone(UTC)
    season_id = build_season_id(competition_id=competition_id, label=season_label)

    competition = CompetitionRecord(
        competition_id=competition_id,
        sport_code=SPORT_FOOTBALL,
        display_name=competition_display_name,
        country_code=country_code,
        competition_type=CompetitionType.DOMESTIC_LEAGUE.value,
        source_name=source_name,
        source_competition_code=source_competition_code,
        timezone=timezone_name,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )
    season = SeasonRecord(
        season_id=season_id,
        competition_id=competition_id,
        label=season_label,
        start_year=start_year,
        end_year=end_year,
        source_season_code=source_season_code,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )

    source_participants = _SourceParticipantRegistry(
        source_name=source_name,
        competition_id=competition_id,
    )
    exact_row_signatures: set[tuple[tuple[str, str], ...]] = set()
    source_event_signatures: dict[str, tuple[tuple[str, str], ...]] = {}
    parsed_events: list[_ParsedSourceEvent] = []
    duplicate_rows_discarded = 0
    warnings: list[str] = []

    for index, row in enumerate(rows, start=2):  # header is row 1; data starts at 2
        signature = tuple(sorted(row.items()))
        if signature in exact_row_signatures:
            duplicate_rows_discarded += 1
            continue

        parsed = _parse_row(
            row=row,
            row_index=index,
            competition_id=competition_id,
            season_id=season_id,
            source_name=source_name,
            timezone_name=timezone_name,
            observed=observed,
            source_file_sha256=source_file_sha256,
            source_participants=source_participants,
        )

        source_event_id = parsed.source_reference.source_event_id
        existing_signature = source_event_signatures.get(source_event_id)
        if existing_signature is not None:
            msg = f"conflicting duplicate source event identity {source_name!r}/{source_event_id!r}"
            raise SourceIntegrityError(msg)

        source_event_signatures[source_event_id] = signature
        exact_row_signatures.add(signature)
        parsed_events.append(parsed)

    participants = _reconcile_participants(
        tuple(source_participants.references()),
        observed=observed,
    )
    participants_by_source_id = {
        participant.source_reference.source_participant_id: participant
        for participant in participants
    }

    event_candidates = tuple(
        _event_reconciliation_candidate(
            parsed,
            participants_by_source_id=participants_by_source_id,
            source_name=source_name,
            observed=observed,
        )
        for parsed in parsed_events
    )
    event_reconciliations = reconcile_candidates(event_candidates)
    reconciliation_by_source_id = {
        reconciliation.source_event_id: reconciliation for reconciliation in event_reconciliations
    }

    source_events = tuple(
        sorted(
            (
                _ingested_source_event(
                    parsed,
                    reconciliation=reconciliation_by_source_id[
                        parsed.source_reference.source_event_id
                    ],
                    participants_by_source_id=participants_by_source_id,
                )
                for parsed in parsed_events
            ),
            key=_source_event_sort_key,
        )
    )
    unresolved_reconciliations = tuple(
        event.reconciliation
        for event in source_events
        if event.reconciliation.state == ReconciliationState.UNRESOLVED.value
    )

    events = _canonical_events_from_sources(source_events)
    canonical_event_ids = {event.canonical.canonical_event_id for event in events}
    resolved_source_events = tuple(
        event
        for event in source_events
        if event.reconciliation.canonical_event_id in canonical_event_ids
    )

    quotes: list[OddsQuote] = []
    statistics_rows: list[PostMatchStatisticsRecord] = []
    pinnacle_caution_quote_count = 0
    for source_event in resolved_source_events:
        assert source_event.source_reference.canonical_event_id is not None
        parsed = next(
            item
            for item in parsed_events
            if item.source_reference.source_event_id
            == source_event.source_reference.source_event_id
        )
        for family in SUPPORTED_ODDS_FAMILIES:
            family_quotes, caution_count = _family_quotes(
                row=parsed.row,
                family=family,
                row_index=parsed.row_number,
                canonical_event_id=source_event.source_reference.canonical_event_id,
                source_name=source_event.source_reference.source_name,
                source_event_id=source_event.source_reference.source_event_id,
                event_date=parsed.event_date,
                observed=observed,
                source_file_sha256=source_file_sha256,
            )
            quotes.extend(family_quotes)
            pinnacle_caution_quote_count += caution_count

        if source_event.status == EventStatus.FINISHED.value:
            statistics = _build_statistics(
                row=parsed.row,
                canonical_event_id=source_event.source_reference.canonical_event_id,
                source_event_id=source_event.source_reference.source_event_id,
                half_time_home_goals=parsed.half_time_home_goals,
                half_time_away_goals=parsed.half_time_away_goals,
                half_time_result=parsed.half_time_result,
                observed=observed,
                source_file_sha256=source_file_sha256,
            )
            if statistics is not None:
                statistics_rows.append(statistics)

    if duplicate_rows_discarded:
        warnings.append(f"discarded_exact_duplicate_rows={duplicate_rows_discarded}")
    if unresolved_reconciliations:
        warnings.append(f"unresolved_events={len(unresolved_reconciliations)}")
    unresolved_participant_count = sum(
        1
        for participant in participants
        if participant.reconciliation.state == ReconciliationState.UNRESOLVED.value
    )
    if unresolved_participant_count:
        warnings.append(f"unresolved_participants={unresolved_participant_count}")

    return NormalizedFootballBundle(
        competitions=(competition,),
        seasons=(season,),
        participants=tuple(sorted(participants, key=_participant_sort_key)),
        source_events=source_events,
        events=events,
        unresolved_reconciliations=unresolved_reconciliations,
        market_quotes=tuple(sorted(quotes, key=quote_sort_key)),
        post_match_statistics=tuple(
            sorted(
                statistics_rows,
                key=lambda item: (item.canonical_event_id, item.source_event_id),
            )
        ),
        duplicate_rows_discarded=duplicate_rows_discarded,
        warnings=tuple(sorted(warnings)),
        pinnacle_caution_quote_count=pinnacle_caution_quote_count,
        source_policy_version=SOURCE_QUALITY_POLICY_VERSION,
        reconciliation_policy_version=RECONCILIATION_POLICY_VERSION,
        participant_reconciliation_policy_version=PARTICIPANT_RECONCILIATION_POLICY_VERSION,
    )


class _SourceParticipantRegistry:
    """Collect unique source participant references before reconciliation."""

    def __init__(self, *, source_name: str, competition_id: str) -> None:
        self._source_name = source_name
        self._competition_id = competition_id
        self._by_source_id: dict[str, SourceParticipantReference] = {}
        self._display_by_normalized_name: dict[str, str] = {}

    def get_or_create(
        self,
        *,
        row_index: int,
        display_name: str,
        normalized_name: str,
    ) -> SourceParticipantReference:
        existing_display = self._display_by_normalized_name.get(normalized_name)
        if existing_display is not None and existing_display != display_name:
            msg = (
                f"row {row_index}: team normalization collision for key "
                f"{normalized_name!r}: {existing_display!r} vs {display_name!r}"
            )
            raise NormalizationError(msg)
        self._display_by_normalized_name[normalized_name] = display_name

        source_key = build_source_participant_key(
            source_name=self._source_name,
            sport_code=SPORT_FOOTBALL,
            competition_id=self._competition_id,
            normalized_name=normalized_name,
        )
        source_id = build_source_participant_id(source_participant_key=source_key)
        reference = SourceParticipantReference(
            source_participant_id=source_id,
            source_name=self._source_name,
            source_participant_key=source_key,
            canonical_participant_id=None,
            competition_id=self._competition_id,
            participant_type=ParticipantType.TEAM.value,
            display_name=display_name,
            normalized_name=normalized_name,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        )

        existing_reference = self._by_source_id.get(source_id)
        if existing_reference is not None:
            if existing_reference != reference:
                msg = (
                    "conflicting duplicate source participant identity "
                    f"{self._source_name!r}/{source_id!r}"
                )
                raise SourceIntegrityError(msg)
            return existing_reference

        self._by_source_id[source_id] = reference
        return reference

    def references(self) -> tuple[SourceParticipantReference, ...]:
        return tuple(
            sorted(
                self._by_source_id.values(),
                key=lambda item: (item.source_name, item.source_participant_id),
            )
        )


def _parse_row(
    *,
    row: dict[str, str],
    row_index: int,
    competition_id: str,
    season_id: str,
    source_name: str,
    timezone_name: str,
    observed: datetime,
    source_file_sha256: str,
    source_participants: _SourceParticipantRegistry,
) -> _ParsedSourceEvent:
    home_display, home_key = normalize_team_name(row.get("HomeTeam", ""))
    away_display, away_key = normalize_team_name(row.get("AwayTeam", ""))
    if home_key == away_key:
        msg = f"row {row_index}: home and away teams must differ"
        raise NormalizationError(msg)

    home_reference = source_participants.get_or_create(
        row_index=row_index,
        display_name=home_display,
        normalized_name=home_key,
    )
    away_reference = source_participants.get_or_create(
        row_index=row_index,
        display_name=away_display,
        normalized_name=away_key,
    )

    event_date = parse_source_date(row.get("Date", ""))
    kickoff = parse_source_time(row.get("Time", ""))
    if kickoff is None:
        scheduled_start_utc = None
        start_time_precision = StartTimePrecision.DATE_ONLY.value
    else:
        scheduled_start_utc = combine_local_kickoff(
            event_date,
            kickoff,
            timezone_name=timezone_name,
        )
        start_time_precision = StartTimePrecision.MINUTE.value

    (
        status,
        full_time_home_goals,
        full_time_away_goals,
        full_time_result,
        outcome_stage,
    ) = _parse_full_time_outcome(row=row, row_index=row_index)
    half_time_home_goals, half_time_away_goals, half_time_result = _parse_half_time_outcome(
        row=row,
        row_index=row_index,
    )

    source_event_key = build_source_event_key(
        source_name=source_name,
        competition_id=competition_id,
        season_id=season_id,
        event_date=event_date,
        home_source_participant_key=home_reference.source_participant_key,
        away_source_participant_key=away_reference.source_participant_key,
    )
    source_event_id = build_source_event_id(source_event_key=source_event_key)
    source_reference = SourceEventReference(
        source_event_id=source_event_id,
        source_name=source_name,
        source_event_key=source_event_key,
        canonical_event_id=None,
        source_row_number=row_index,
        source_observed_at_utc=observed,
        source_file_sha256=source_file_sha256,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )

    return _ParsedSourceEvent(
        row=row,
        row_number=row_index,
        source_reference=source_reference,
        competition_id=competition_id,
        season_id=season_id,
        event_occurrence_key=FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
        event_date=event_date,
        scheduled_start_utc=scheduled_start_utc,
        start_time_precision=start_time_precision,
        status=status,
        home_source_participant_id=home_reference.source_participant_id,
        away_source_participant_id=away_reference.source_participant_id,
        home_score=full_time_home_goals,
        away_score=full_time_away_goals,
        result_code=full_time_result,
        outcome_availability_stage=outcome_stage,
        half_time_home_goals=half_time_home_goals,
        half_time_away_goals=half_time_away_goals,
        half_time_result=half_time_result,
    )


def _parse_full_time_outcome(
    *,
    row: dict[str, str],
    row_index: int,
) -> tuple[str, int | None, int | None, str | None, str]:
    fthg = parse_optional_int(row.get("FTHG", ""), field_name="FTHG", maximum=MAX_GOALS)
    ftag = parse_optional_int(row.get("FTAG", ""), field_name="FTAG", maximum=MAX_GOALS)
    ftr_raw = row.get("FTR", "").strip()
    if (fthg is None) != (ftag is None):
        msg = f"row {row_index}: FTHG/FTAG must both be present or both empty"
        raise NormalizationError(msg)
    if fthg is None:
        if ftr_raw:
            msg = f"row {row_index}: FTR must be empty for scheduled games"
            raise NormalizationError(msg)
        return (
            EventStatus.SCHEDULED.value,
            None,
            None,
            None,
            OutcomeAvailability.PRE_EVENT_UNAVAILABLE.value,
        )

    if not ftr_raw:
        msg = f"row {row_index}: FTR is required for finished games"
        raise NormalizationError(msg)
    full_time_result = map_result_code(ftr_raw, field_name="FTR")
    assert ftag is not None
    if full_time_result != expected_result_from_goals(fthg, ftag):
        msg = f"row {row_index}: FTR inconsistent with FTHG/FTAG"
        raise NormalizationError(msg)
    return (
        EventStatus.FINISHED.value,
        fthg,
        ftag,
        full_time_result,
        OutcomeAvailability.POST_EVENT.value,
    )


def _parse_half_time_outcome(
    *,
    row: dict[str, str],
    row_index: int,
) -> tuple[int | None, int | None, str | None]:
    hthg = parse_optional_int(row.get("HTHG", ""), field_name="HTHG", maximum=MAX_GOALS)
    htag = parse_optional_int(row.get("HTAG", ""), field_name="HTAG", maximum=MAX_GOALS)
    htr_raw = row.get("HTR", "").strip()
    if (hthg is None) != (htag is None):
        msg = f"row {row_index}: HTHG/HTAG must both be present or both empty"
        raise NormalizationError(msg)
    if hthg is None:
        if htr_raw:
            msg = f"row {row_index}: HTR must be empty when half-time goals are absent"
            raise NormalizationError(msg)
        return None, None, None

    if not htr_raw:
        msg = f"row {row_index}: HTR is required when half-time goals are present"
        raise NormalizationError(msg)
    half_time_result = map_result_code(htr_raw, field_name="HTR")
    assert htag is not None
    if half_time_result != expected_result_from_goals(hthg, htag):
        msg = f"row {row_index}: HTR inconsistent with HTHG/HTAG"
        raise NormalizationError(msg)
    return hthg, htag, half_time_result


def _reconcile_participants(
    references: tuple[SourceParticipantReference, ...],
    *,
    observed: datetime,
) -> tuple[IngestedParticipant, ...]:
    candidates = tuple(
        ParticipantReconciliationCandidate(
            source_name=reference.source_name,
            source_participant_id=reference.source_participant_id,
            source_participant_key=reference.source_participant_key,
            sport_code=SPORT_FOOTBALL,
            competition_id=reference.competition_id,
            participant_type=reference.participant_type,
            normalized_name=reference.normalized_name,
            display_name=reference.display_name,
            source_observed_at_utc=observed,
            schema_version=reference.schema_version,
        )
        for reference in references
    )
    reconciliations = reconcile_participant_candidates(candidates)
    reconciliation_by_source_id = {
        reconciliation.source_participant_id: reconciliation for reconciliation in reconciliations
    }

    participants: list[IngestedParticipant] = []
    for reference in references:
        reconciliation = reconciliation_by_source_id[reference.source_participant_id]
        canonical: CanonicalParticipant | None = None
        source_reference = reference
        if reconciliation.state == ReconciliationState.EXACT.value:
            canonical_id = build_canonical_participant_id(
                sport_code=SPORT_FOOTBALL,
                competition_id=reference.competition_id,
                participant_type=reference.participant_type,
                canonical_key=reference.normalized_name,
            )
            if reconciliation.canonical_participant_id != canonical_id:
                msg = "participant reconciliation canonical id does not match canonical key"
                raise NormalizationError(msg)
            canonical = CanonicalParticipant(
                canonical_participant_id=canonical_id,
                sport_code=SPORT_FOOTBALL,
                competition_id=reference.competition_id,
                participant_type=reference.participant_type,
                canonical_key=reference.normalized_name,
                display_name=reference.display_name,
                schema_version=reference.schema_version,
            )
            source_reference = replace(reference, canonical_participant_id=canonical_id)
        participants.append(
            IngestedParticipant(
                source_reference=source_reference,
                reconciliation=reconciliation,
                canonical=canonical,
            )
        )
    return tuple(participants)


def _event_reconciliation_candidate(
    parsed: _ParsedSourceEvent,
    *,
    participants_by_source_id: dict[str, IngestedParticipant],
    source_name: str,
    observed: datetime,
) -> ReconciliationCandidate:
    home = participants_by_source_id[parsed.home_source_participant_id]
    away = participants_by_source_id[parsed.away_source_participant_id]
    home_canonical_id = _exact_canonical_participant_id(home)
    away_canonical_id = _exact_canonical_participant_id(away)
    return ReconciliationCandidate(
        source_name=source_name,
        source_event_id=parsed.source_reference.source_event_id,
        source_event_key=parsed.source_reference.source_event_key,
        sport_code=SPORT_FOOTBALL,
        competition_id=parsed.competition_id,
        season_id=parsed.season_id,
        event_occurrence_key=parsed.event_occurrence_key,
        event_date=parsed.event_date,
        scheduled_start_utc=parsed.scheduled_start_utc,
        home_canonical_participant_id=home_canonical_id,
        away_canonical_participant_id=away_canonical_id,
        source_observed_at_utc=observed,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )


def _ingested_source_event(
    parsed: _ParsedSourceEvent,
    *,
    reconciliation: EventReconciliation,
    participants_by_source_id: dict[str, IngestedParticipant],
) -> IngestedSourceEvent:
    home = participants_by_source_id[parsed.home_source_participant_id]
    away = participants_by_source_id[parsed.away_source_participant_id]
    home_canonical_id = _exact_canonical_participant_id(home)
    away_canonical_id = _exact_canonical_participant_id(away)
    canonical_event_id = (
        reconciliation.canonical_event_id if reconciliation.is_downstream_safe else None
    )
    source_reference = replace(
        parsed.source_reference,
        canonical_event_id=canonical_event_id,
    )
    return IngestedSourceEvent(
        source_reference=source_reference,
        reconciliation=reconciliation,
        competition_id=parsed.competition_id,
        season_id=parsed.season_id,
        event_occurrence_key=parsed.event_occurrence_key,
        event_date=parsed.event_date,
        scheduled_start_utc=parsed.scheduled_start_utc,
        start_time_precision=parsed.start_time_precision,
        status=parsed.status,
        home_source_participant_id=parsed.home_source_participant_id,
        away_source_participant_id=parsed.away_source_participant_id,
        home_canonical_participant_id=home_canonical_id,
        away_canonical_participant_id=away_canonical_id,
        home_score=parsed.home_score,
        away_score=parsed.away_score,
        result_code=parsed.result_code,
        outcome_availability_stage=parsed.outcome_availability_stage,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )


def _canonical_events_from_sources(
    source_events: tuple[IngestedSourceEvent, ...],
) -> tuple[IngestedEvent, ...]:
    selected_by_canonical_id: dict[str, IngestedSourceEvent] = {}
    for source_event in source_events:
        canonical_event_id = source_event.reconciliation.canonical_event_id
        if not source_event.reconciliation.is_downstream_safe or canonical_event_id is None:
            continue
        current = selected_by_canonical_id.get(canonical_event_id)
        if current is None or _canonical_metadata_preference_key(source_event) < (
            _canonical_metadata_preference_key(current)
        ):
            selected_by_canonical_id[canonical_event_id] = source_event

    events = [
        IngestedEvent(
            canonical=_canonical_event_from_source(source_event),
            reconciliation=source_event.reconciliation,
        )
        for source_event in selected_by_canonical_id.values()
    ]
    return tuple(sorted(events, key=_event_sort_key))


def _canonical_event_from_source(source_event: IngestedSourceEvent) -> CanonicalEvent:
    canonical_event_id = source_event.reconciliation.canonical_event_id
    if canonical_event_id is None:
        msg = "cannot build canonical event from unresolved source event"
        raise NormalizationError(msg)
    if source_event.event_occurrence_key is None:
        msg = "resolved source event missing event occurrence key"
        raise NormalizationError(msg)
    if source_event.event_date is None:
        msg = "resolved source event missing event date"
        raise NormalizationError(msg)
    if (
        source_event.home_canonical_participant_id is None
        or source_event.away_canonical_participant_id is None
    ):
        msg = "resolved source event missing canonical participants"
        raise NormalizationError(msg)
    expected_id = build_canonical_event_id(
        sport_code=SPORT_FOOTBALL,
        competition_id=source_event.competition_id,
        season_id=source_event.season_id,
        home_canonical_participant_id=source_event.home_canonical_participant_id,
        away_canonical_participant_id=source_event.away_canonical_participant_id,
        event_occurrence_key=source_event.event_occurrence_key,
    )
    if canonical_event_id != expected_id:
        msg = "event reconciliation canonical id does not match canonical event key"
        raise NormalizationError(msg)
    return CanonicalEvent(
        canonical_event_id=canonical_event_id,
        sport_code=SPORT_FOOTBALL,
        competition_id=source_event.competition_id,
        season_id=source_event.season_id,
        event_occurrence_key=source_event.event_occurrence_key,
        event_date=source_event.event_date,
        scheduled_start_utc=source_event.scheduled_start_utc,
        start_time_precision=source_event.start_time_precision,
        status=source_event.status,
        home_canonical_participant_id=source_event.home_canonical_participant_id,
        away_canonical_participant_id=source_event.away_canonical_participant_id,
        home_score=source_event.home_score,
        away_score=source_event.away_score,
        result_code=source_event.result_code,
        outcome_availability_stage=source_event.outcome_availability_stage,
        schema_version=source_event.schema_version,
    )


def _normalize_referee(value: str) -> str | None:
    text = value.strip()
    if text == "":
        return None
    if "\x00" in text:
        msg = "referee must not contain NUL"
        raise NormalizationError(msg)
    normalized = unicodedata.normalize("NFC", " ".join(text.split()))
    if len(normalized) > MAX_REFEREE_LENGTH:
        msg = f"referee exceeds maximum length of {MAX_REFEREE_LENGTH}"
        raise NormalizationError(msg)
    return normalized


def _pinnacle_quality(event_date: date) -> tuple[str, str | None]:
    if event_date >= PINNACLE_CAUTION_CUTOFF:
        return (
            QuoteQualityStatus.CAUTION.value,
            "upstream source reports reliability concerns for Pinnacle fields "
            f"on or after {PINNACLE_CAUTION_CUTOFF.isoformat()}",
        )
    return QuoteQualityStatus.SOURCE_PROVIDED.value, None


def _family_quality(family: OddsColumnFamily, event_date: date) -> tuple[str, str | None]:
    if family.provider_id == "pinnacle":
        return _pinnacle_quality(event_date)
    if family.provider_type.startswith("source-market"):
        return QuoteQualityStatus.SOURCE_PROVIDED_AGGREGATE.value, None
    return QuoteQualityStatus.SOURCE_PROVIDED.value, None


def _family_quotes(
    *,
    row: dict[str, str],
    family: OddsColumnFamily,
    row_index: int,
    canonical_event_id: str,
    source_name: str,
    source_event_id: str,
    event_date: date,
    observed: datetime,
    source_file_sha256: str,
) -> tuple[list[OddsQuote], int]:
    parsed = {
        outcome: parse_decimal_odds(
            row.get(family.column_for(outcome), ""),
            field_name=family.column_for(outcome),
        )
        for outcome in MATCH_RESULT_1X2_OUTCOMES
    }
    present = [value is not None for value in parsed.values()]
    if not any(present):
        return [], 0
    if not all(present):
        msg = f"row {row_index}: odds family {family.family_id} requires a complete H/D/A triple"
        raise NormalizationError(msg)

    quality_status, quality_reason = _family_quality(family, event_date)
    caution = 0
    if quality_status == QuoteQualityStatus.CAUTION.value:
        caution = len(MATCH_RESULT_1X2_OUTCOMES)

    quotes: list[OddsQuote] = []
    for outcome in MATCH_RESULT_1X2_OUTCOMES:
        odds_value = parsed[outcome]
        assert odds_value is not None
        selection = match_result_1x2_selection(outcome)
        column = family.column_for(outcome)
        quote_series_id = build_quote_series_id(
            canonical_event_id=canonical_event_id,
            selection=selection,
            provider_type=family.provider_type,
            provider_id=family.provider_id,
        )
        quote_observation_id = build_quote_observation_id(
            quote_series_id=quote_series_id,
            source_name=source_name,
            source_event_id=source_event_id,
            selection=selection,
            provider_type=family.provider_type,
            provider_id=family.provider_id,
            quote_phase=family.quote_phase,
            source_observed_at_utc=observed,
            quoted_at_utc=None,
            source_file_sha256=source_file_sha256,
            source_field=column,
        )
        quotes.append(
            OddsQuote(
                quote_series_id=quote_series_id,
                quote_observation_id=quote_observation_id,
                canonical_event_id=canonical_event_id,
                source_name=source_name,
                source_event_id=source_event_id,
                selection=selection,
                provider_type=family.provider_type,
                provider_id=family.provider_id,
                decimal_odds=odds_value,
                quote_phase=family.quote_phase,
                source_observed_at_utc=observed,
                # Football-Data.co.uk publishes no original quote timestamp.
                quoted_at_utc=None,
                quote_timestamp_precision=(QuoteTimestampPrecision.SNAPSHOT_OBSERVATION_ONLY.value),
                quote_valid_from_utc=None,
                quote_valid_to_utc=None,
                market_status=MarketStatus.UNKNOWN.value,
                selection_status=SelectionStatus.UNKNOWN.value,
                source_field=column,
                quality_status=quality_status,
                quality_reason=quality_reason,
                source_file_sha256=source_file_sha256,
                schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
            )
        )
    return quotes, caution


def _build_statistics(
    *,
    row: dict[str, str],
    canonical_event_id: str,
    source_event_id: str,
    half_time_home_goals: int | None,
    half_time_away_goals: int | None,
    half_time_result: str | None,
    observed: datetime,
    source_file_sha256: str,
) -> PostMatchStatisticsRecord | None:
    referee = _normalize_referee(row.get("Referee", ""))
    hs, as_ = parse_required_pair(
        row.get("HS", ""),
        row.get("AS", ""),
        home_field="HS",
        away_field="AS",
        maximum=MAX_SHOTS,
    )
    hst, ast = parse_required_pair(
        row.get("HST", ""),
        row.get("AST", ""),
        home_field="HST",
        away_field="AST",
        maximum=MAX_SHOTS,
    )
    hc, ac = parse_required_pair(
        row.get("HC", ""),
        row.get("AC", ""),
        home_field="HC",
        away_field="AC",
        maximum=MAX_CORNERS,
    )
    hf, af = parse_required_pair(
        row.get("HF", ""),
        row.get("AF", ""),
        home_field="HF",
        away_field="AF",
        maximum=MAX_FOULS,
    )
    hy, ay = parse_required_pair(
        row.get("HY", ""),
        row.get("AY", ""),
        home_field="HY",
        away_field="AY",
        maximum=MAX_CARDS,
    )
    hr, ar = parse_required_pair(
        row.get("HR", ""),
        row.get("AR", ""),
        home_field="HR",
        away_field="AR",
        maximum=MAX_CARDS,
    )
    values = (
        referee,
        hs,
        as_,
        hst,
        ast,
        hc,
        ac,
        hf,
        af,
        hy,
        ay,
        hr,
        ar,
        half_time_home_goals,
        half_time_away_goals,
    )
    if not any(value is not None for value in values):
        return None
    return PostMatchStatisticsRecord(
        canonical_event_id=canonical_event_id,
        source_event_id=source_event_id,
        availability_stage=AVAILABILITY_POST_MATCH,
        half_time_home_goals=half_time_home_goals,
        half_time_away_goals=half_time_away_goals,
        half_time_result=half_time_result,
        referee=referee,
        home_shots=hs,
        away_shots=as_,
        home_shots_on_target=hst,
        away_shots_on_target=ast,
        home_corners=hc,
        away_corners=ac,
        home_fouls=hf,
        away_fouls=af,
        home_yellow_cards=hy,
        away_yellow_cards=ay,
        home_red_cards=hr,
        away_red_cards=ar,
        source_observed_at_utc=observed,
        source_file_sha256=source_file_sha256,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )


def _participant_sort_key(participant: IngestedParticipant) -> tuple[str, str]:
    return (
        participant.source_reference.source_name,
        participant.source_reference.source_participant_id,
    )


def _exact_canonical_participant_id(participant: IngestedParticipant) -> str | None:
    if (
        participant.reconciliation.state == ReconciliationState.EXACT.value
        and participant.canonical is not None
    ):
        return participant.canonical.canonical_participant_id
    return None


def _source_event_sort_key(event: IngestedSourceEvent) -> tuple[str, str]:
    return (
        event.source_reference.source_name,
        event.source_reference.source_event_id,
    )


def _event_sort_key(event: IngestedEvent) -> tuple[int, int, str, str, str, str]:
    scheduled = event.canonical.scheduled_start_utc
    return (
        event.canonical.event_date.toordinal(),
        1 if scheduled is None else 0,
        format_utc_timestamp(scheduled) if scheduled is not None else "",
        event.canonical.home_canonical_participant_id,
        event.canonical.away_canonical_participant_id,
        event.canonical.canonical_event_id,
    )


def _canonical_metadata_preference_key(
    source_event: IngestedSourceEvent,
) -> tuple[int, int, str, str]:
    if source_event.event_date is None:
        event_date_ordinal = date.max.toordinal()
    else:
        event_date_ordinal = source_event.event_date.toordinal()
    scheduled = source_event.scheduled_start_utc
    return (
        event_date_ordinal,
        1 if scheduled is None else 0,
        format_utc_timestamp(scheduled) if scheduled is not None else "",
        source_event.source_reference.source_name,
    )

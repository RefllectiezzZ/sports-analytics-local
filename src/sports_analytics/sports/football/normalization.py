"""Normalize Football-Data.co.uk rows into canonical football-canonical-v2 records."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.markets.contracts import (
    MarketStatus,
    OddsQuote,
    QuoteQualityStatus,
    QuoteTimestampPrecision,
    SelectionStatus,
)
from sports_analytics.markets.identifiers import build_quote_id
from sports_analytics.markets.schemas import quote_sort_key
from sports_analytics.sports.contracts import (
    CanonicalEvent,
    CanonicalParticipant,
    CompetitionRecord,
    EventReconciliation,
    EventStatus,
    IngestedEvent,
    IngestedParticipant,
    OutcomeAvailability,
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
    RECONCILIATION_POLICY_VERSION,
    ReconciliationCandidate,
    reconcile_candidates,
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
    """Canonical football datasets produced by one ingestion."""

    competitions: tuple[CompetitionRecord, ...]
    seasons: tuple[SeasonRecord, ...]
    participants: tuple[IngestedParticipant, ...]
    events: tuple[IngestedEvent, ...]
    unresolved_reconciliations: tuple[EventReconciliation, ...]
    market_quotes: tuple[OddsQuote, ...]
    post_match_statistics: tuple[PostMatchStatisticsRecord, ...]
    duplicate_rows_discarded: int
    warnings: tuple[str, ...]
    pinnacle_caution_quote_count: int
    source_policy_version: str
    reconciliation_policy_version: str

    @property
    def reconciliations(self) -> tuple[EventReconciliation, ...]:
        """Return every reconciliation decision, resolved and unresolved."""
        combined = [event.reconciliation for event in self.events]
        combined.extend(self.unresolved_reconciliations)
        return tuple(sorted(combined, key=lambda item: (item.source_name, item.source_event_id)))


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


@dataclass(frozen=True, slots=True)
class _RowIdentity:
    source_event_key: str
    source_event_id: str
    canonical_event_id: str
    home_canonical_participant_id: str
    away_canonical_participant_id: str
    event_date: date
    scheduled_start_utc: datetime | None


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
    """Normalize parsed CSV rows into sorted canonical football datasets."""
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

    participants_by_key: dict[str, IngestedParticipant] = {}
    display_by_key: dict[str, str] = {}
    canonical_events: dict[str, CanonicalEvent] = {}
    source_references: dict[str, SourceEventReference] = {}
    candidates: list[ReconciliationCandidate] = []
    exact_row_signatures: set[tuple[tuple[str, str], ...]] = set()
    duplicate_rows_discarded = 0
    warnings: list[str] = []
    quotes: list[OddsQuote] = []
    statistics_rows: list[PostMatchStatisticsRecord] = []
    pinnacle_caution_quote_count = 0

    for index, row in enumerate(rows, start=2):  # header is row 1; data starts at 2
        signature = tuple(sorted(row.items()))
        if signature in exact_row_signatures:
            duplicate_rows_discarded += 1
            continue

        home_display, home_key = normalize_team_name(row.get("HomeTeam", ""))
        away_display, away_key = normalize_team_name(row.get("AwayTeam", ""))
        if home_key == away_key:
            msg = f"row {index}: home and away teams must differ"
            raise NormalizationError(msg)

        for display, key in ((home_display, home_key), (away_display, away_key)):
            existing_display = display_by_key.get(key)
            if existing_display is not None and existing_display != display:
                msg = (
                    f"row {index}: team normalization collision for key {key!r}: "
                    f"{existing_display!r} vs {display!r}"
                )
                raise NormalizationError(msg)
            display_by_key[key] = display
            if key not in participants_by_key:
                participants_by_key[key] = _build_participant(
                    source_name=source_name,
                    normalized_key=key,
                    display_name=display,
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

        fthg = parse_optional_int(row.get("FTHG", ""), field_name="FTHG", maximum=MAX_GOALS)
        ftag = parse_optional_int(row.get("FTAG", ""), field_name="FTAG", maximum=MAX_GOALS)
        ftr_raw = row.get("FTR", "").strip()
        if (fthg is None) != (ftag is None):
            msg = f"row {index}: FTHG/FTAG must both be present or both empty"
            raise NormalizationError(msg)
        if fthg is None:
            status = EventStatus.SCHEDULED.value
            full_time_result = None
            outcome_stage = OutcomeAvailability.PRE_EVENT_UNAVAILABLE.value
            if ftr_raw:
                msg = f"row {index}: FTR must be empty for scheduled games"
                raise NormalizationError(msg)
        else:
            status = EventStatus.FINISHED.value
            outcome_stage = OutcomeAvailability.POST_EVENT.value
            if not ftr_raw:
                msg = f"row {index}: FTR is required for finished games"
                raise NormalizationError(msg)
            full_time_result = map_result_code(ftr_raw, field_name="FTR")
            assert ftag is not None
            expected = expected_result_from_goals(fthg, ftag)
            if full_time_result != expected:
                msg = f"row {index}: FTR inconsistent with FTHG/FTAG"
                raise NormalizationError(msg)

        hthg = parse_optional_int(row.get("HTHG", ""), field_name="HTHG", maximum=MAX_GOALS)
        htag = parse_optional_int(row.get("HTAG", ""), field_name="HTAG", maximum=MAX_GOALS)
        htr_raw = row.get("HTR", "").strip()
        if (hthg is None) != (htag is None):
            msg = f"row {index}: HTHG/HTAG must both be present or both empty"
            raise NormalizationError(msg)
        half_time_result: str | None
        if hthg is None:
            half_time_result = None
            if htr_raw:
                msg = f"row {index}: HTR must be empty when half-time goals are absent"
                raise NormalizationError(msg)
        else:
            if not htr_raw:
                msg = f"row {index}: HTR is required when half-time goals are present"
                raise NormalizationError(msg)
            half_time_result = map_result_code(htr_raw, field_name="HTR")
            assert htag is not None
            if half_time_result != expected_result_from_goals(hthg, htag):
                msg = f"row {index}: HTR inconsistent with HTHG/HTAG"
                raise NormalizationError(msg)

        home_participant = participants_by_key[home_key]
        away_participant = participants_by_key[away_key]
        identity = _build_row_identity(
            source_name=source_name,
            competition_id=competition_id,
            season_id=season_id,
            event_date=event_date,
            scheduled_start_utc=scheduled_start_utc,
            home=home_participant,
            away=away_participant,
        )
        if identity.source_event_id in source_references:
            # Two rows of one source file describing the same fixture with different
            # content is a source-integrity failure, not a reconciliation ambiguity.
            msg = f"row {index}: conflicting duplicate source game key"
            raise NormalizationError(msg)

        canonical_events[identity.source_event_id] = CanonicalEvent(
            canonical_event_id=identity.canonical_event_id,
            sport_code=SPORT_FOOTBALL,
            competition_id=competition_id,
            season_id=season_id,
            event_date=event_date,
            scheduled_start_utc=scheduled_start_utc,
            start_time_precision=start_time_precision,
            status=status,
            home_canonical_participant_id=identity.home_canonical_participant_id,
            away_canonical_participant_id=identity.away_canonical_participant_id,
            home_score=fthg,
            away_score=ftag,
            result_code=full_time_result,
            outcome_availability_stage=outcome_stage,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        )
        source_references[identity.source_event_id] = SourceEventReference(
            source_event_id=identity.source_event_id,
            source_name=source_name,
            source_event_key=identity.source_event_key,
            canonical_event_id=identity.canonical_event_id,
            source_row_number=index,
            source_observed_at_utc=observed,
            source_file_sha256=source_file_sha256,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        )
        candidates.append(
            ReconciliationCandidate(
                source_name=source_name,
                source_event_id=identity.source_event_id,
                source_event_key=identity.source_event_key,
                sport_code=SPORT_FOOTBALL,
                competition_id=competition_id,
                season_id=season_id,
                event_date=event_date,
                scheduled_start_utc=scheduled_start_utc,
                home_canonical_participant_id=identity.home_canonical_participant_id,
                away_canonical_participant_id=identity.away_canonical_participant_id,
                source_observed_at_utc=observed,
                schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
            )
        )
        exact_row_signatures.add(signature)

        for family in SUPPORTED_ODDS_FAMILIES:
            family_quotes, caution_count = _family_quotes(
                row=row,
                family=family,
                row_index=index,
                canonical_event_id=identity.canonical_event_id,
                source_event_id=identity.source_event_id,
                event_date=event_date,
                observed=observed,
                source_file_sha256=source_file_sha256,
            )
            quotes.extend(family_quotes)
            pinnacle_caution_quote_count += caution_count

        if status == EventStatus.FINISHED.value:
            statistics = _build_statistics(
                row=row,
                canonical_event_id=identity.canonical_event_id,
                source_event_id=identity.source_event_id,
                half_time_home_goals=hthg,
                half_time_away_goals=htag,
                half_time_result=half_time_result,
                observed=observed,
                source_file_sha256=source_file_sha256,
            )
            if statistics is not None:
                statistics_rows.append(statistics)

    if duplicate_rows_discarded:
        warnings.append(f"discarded_exact_duplicate_rows={duplicate_rows_discarded}")

    reconciliations = reconcile_candidates(tuple(candidates))
    events: list[IngestedEvent] = []
    unresolved: list[EventReconciliation] = []
    for reconciliation in reconciliations:
        if reconciliation.state == ReconciliationState.UNRESOLVED.value:
            unresolved.append(reconciliation)
            continue
        canonical = canonical_events[reconciliation.source_event_id]
        events.append(
            IngestedEvent(
                canonical=canonical,
                source_reference=source_references[reconciliation.source_event_id],
                reconciliation=reconciliation,
            )
        )
    if unresolved:
        warnings.append(f"unresolved_events={len(unresolved)}")

    resolved_event_ids = {event.source_reference.source_event_id for event in events}
    quotes = [quote for quote in quotes if quote.source_event_id in resolved_event_ids]
    statistics_rows = [
        item for item in statistics_rows if item.source_event_id in resolved_event_ids
    ]

    sorted_events = sorted(
        events,
        key=lambda item: (
            item.canonical.event_date.toordinal(),
            # Null scheduled_start_utc sorts after non-null (documented policy).
            1 if item.canonical.scheduled_start_utc is None else 0,
            format_utc_timestamp(item.canonical.scheduled_start_utc)
            if item.canonical.scheduled_start_utc is not None
            else "",
            item.canonical.home_canonical_participant_id,
            item.canonical.away_canonical_participant_id,
            item.canonical.canonical_event_id,
        ),
    )
    sorted_participants = sorted(
        participants_by_key.values(),
        key=lambda item: item.canonical.canonical_participant_id,
    )
    sorted_quotes = sorted(quotes, key=quote_sort_key)
    sorted_statistics = sorted(statistics_rows, key=lambda item: item.canonical_event_id)

    return NormalizedFootballBundle(
        competitions=(competition,),
        seasons=(season,),
        participants=tuple(sorted_participants),
        events=tuple(sorted_events),
        unresolved_reconciliations=tuple(unresolved),
        market_quotes=tuple(sorted_quotes),
        post_match_statistics=tuple(sorted_statistics),
        duplicate_rows_discarded=duplicate_rows_discarded,
        warnings=tuple(sorted(warnings)),
        pinnacle_caution_quote_count=pinnacle_caution_quote_count,
        source_policy_version=SOURCE_QUALITY_POLICY_VERSION,
        reconciliation_policy_version=RECONCILIATION_POLICY_VERSION,
    )


def _build_participant(
    *,
    source_name: str,
    normalized_key: str,
    display_name: str,
) -> IngestedParticipant:
    canonical_id = build_canonical_participant_id(
        sport_code=SPORT_FOOTBALL,
        participant_type=ParticipantType.TEAM.value,
        canonical_key=normalized_key,
    )
    source_key = build_source_participant_key(
        source_name=source_name,
        sport_code=SPORT_FOOTBALL,
        normalized_name=normalized_key,
    )
    return IngestedParticipant(
        canonical=CanonicalParticipant(
            canonical_participant_id=canonical_id,
            sport_code=SPORT_FOOTBALL,
            participant_type=ParticipantType.TEAM.value,
            canonical_key=normalized_key,
            display_name=display_name,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        ),
        source_reference=SourceParticipantReference(
            source_participant_id=build_source_participant_id(source_participant_key=source_key),
            source_name=source_name,
            source_participant_key=source_key,
            canonical_participant_id=canonical_id,
            participant_type=ParticipantType.TEAM.value,
            display_name=display_name,
            normalized_name=normalized_key,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        ),
    )


def _build_row_identity(
    *,
    source_name: str,
    competition_id: str,
    season_id: str,
    event_date: date,
    scheduled_start_utc: datetime | None,
    home: IngestedParticipant,
    away: IngestedParticipant,
) -> _RowIdentity:
    source_event_key = build_source_event_key(
        source_name=source_name,
        competition_id=competition_id,
        season_id=season_id,
        event_date=event_date,
        home_source_participant_key=home.source_reference.source_participant_key,
        away_source_participant_key=away.source_reference.source_participant_key,
    )
    canonical_event_id = build_canonical_event_id(
        sport_code=SPORT_FOOTBALL,
        competition_id=competition_id,
        season_id=season_id,
        event_date=event_date,
        home_canonical_participant_id=home.canonical.canonical_participant_id,
        away_canonical_participant_id=away.canonical.canonical_participant_id,
    )
    return _RowIdentity(
        source_event_key=source_event_key,
        source_event_id=build_source_event_id(source_event_key=source_event_key),
        canonical_event_id=canonical_event_id,
        home_canonical_participant_id=home.canonical.canonical_participant_id,
        away_canonical_participant_id=away.canonical.canonical_participant_id,
        event_date=event_date,
        scheduled_start_utc=scheduled_start_utc,
    )


def _family_quotes(
    *,
    row: dict[str, str],
    family: OddsColumnFamily,
    row_index: int,
    canonical_event_id: str,
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
        quote_id = build_quote_id(
            canonical_event_id=canonical_event_id,
            selection=selection,
            provider_type=family.provider_type,
            provider_id=family.provider_id,
            quote_phase=family.quote_phase,
            source_field=column,
        )
        quotes.append(
            OddsQuote(
                quote_id=quote_id,
                canonical_event_id=canonical_event_id,
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

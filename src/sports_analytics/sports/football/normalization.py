"""Normalize Football-Data.co.uk rows into football-canonical-v1 records."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.sports.football.contracts import FOOTBALL_CANONICAL_SCHEMA_VERSION
from sports_analytics.sports.football.identifiers import (
    build_game_id,
    build_quote_id,
    build_season_id,
    build_source_game_key,
    build_team_id,
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
from sports_analytics.sports.identifiers import SPORT_FOOTBALL
from sports_analytics.sports.types import CompetitionType

PINNACLE_CAUTION_CUTOFF: Final[date] = date(2025, 7, 23)
SOURCE_QUALITY_POLICY_VERSION: Final[str] = "football-data-co-uk-policy-v1"
MARKET_TYPE_1X2: Final[str] = "match-result-1x2"
QUOTE_TIMESTAMP_PRECISION: Final[str] = "snapshot-observation-only"
AVAILABILITY_POST_MATCH: Final[str] = "post-match"


@dataclass(frozen=True, slots=True)
class CompetitionRecord:
    competition_id: str
    sport_code: str
    display_name: str
    country_code: str
    competition_type: str
    source_name: str
    source_competition_code: str
    timezone: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class SeasonRecord:
    season_id: str
    competition_id: str
    label: str
    start_year: int
    end_year: int
    source_season_code: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class TeamRecord:
    team_id: str
    sport_code: str
    source_name: str
    source_team_key: str
    display_name: str
    normalized_name: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class GameRecord:
    game_id: str
    sport_code: str
    competition_id: str
    season_id: str
    source_name: str
    source_game_key: str
    source_row_number: int
    event_date: date
    scheduled_start_utc: datetime | None
    start_time_precision: str
    status: str
    home_team_id: str
    away_team_id: str
    full_time_home_goals: int | None
    full_time_away_goals: int | None
    full_time_result: str | None
    half_time_home_goals: int | None
    half_time_away_goals: int | None
    half_time_result: str | None
    source_observed_at_utc: datetime
    source_file_sha256: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class OddsQuoteRecord:
    quote_id: str
    game_id: str
    market_type: str
    selection: str
    provider_type: str
    provider_id: str
    quote_phase: str
    decimal_odds: Decimal
    source_column: str
    quoted_at_utc: datetime | None
    source_observed_at_utc: datetime
    quote_timestamp_precision: str
    source_file_sha256: str
    quality_status: str
    quality_reason: str | None
    schema_version: str


@dataclass(frozen=True, slots=True)
class PostMatchStatisticsRecord:
    game_id: str
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
    availability_stage: str
    source_observed_at_utc: datetime
    source_file_sha256: str
    schema_version: str


@dataclass(frozen=True, slots=True)
class OddsColumnFamily:
    provider_type: str
    provider_id: str
    quote_phase: str
    home_column: str
    draw_column: str
    away_column: str
    family_id: str


# Explicit supported odds column families only.
SUPPORTED_ODDS_FAMILIES: Final[tuple[OddsColumnFamily, ...]] = (
    OddsColumnFamily("bookmaker", "bet365", "opening", "B365H", "B365D", "B365A", "b365-opening"),
    OddsColumnFamily(
        "bookmaker", "bet365", "closing", "B365CH", "B365CD", "B365CA", "b365-closing"
    ),
    OddsColumnFamily("bookmaker", "pinnacle", "opening", "PSH", "PSD", "PSA", "pinnacle-opening"),
    OddsColumnFamily(
        "bookmaker", "pinnacle", "closing", "PSCH", "PSCD", "PSCA", "pinnacle-closing"
    ),
    OddsColumnFamily(
        "source-market-average",
        "market-average",
        "opening",
        "AvgH",
        "AvgD",
        "AvgA",
        "avg-opening",
    ),
    OddsColumnFamily(
        "source-market-average",
        "market-average",
        "closing",
        "AvgCH",
        "AvgCD",
        "AvgCA",
        "avg-closing",
    ),
    OddsColumnFamily(
        "source-market-maximum",
        "market-maximum",
        "opening",
        "MaxH",
        "MaxD",
        "MaxA",
        "max-opening",
    ),
    OddsColumnFamily(
        "source-market-maximum",
        "market-maximum",
        "closing",
        "MaxCH",
        "MaxCD",
        "MaxCA",
        "max-closing",
    ),
)

SUPPORTED_ODDS_COLUMNS: Final[frozenset[str]] = frozenset(
    column
    for family in SUPPORTED_ODDS_FAMILIES
    for column in (family.home_column, family.draw_column, family.away_column)
)


@dataclass(frozen=True, slots=True)
class NormalizedFootballBundle:
    competitions: tuple[CompetitionRecord, ...]
    seasons: tuple[SeasonRecord, ...]
    teams: tuple[TeamRecord, ...]
    games: tuple[GameRecord, ...]
    odds_1x2: tuple[OddsQuoteRecord, ...]
    post_match_statistics: tuple[PostMatchStatisticsRecord, ...]
    duplicate_rows_discarded: int
    warnings: tuple[str, ...]
    pinnacle_caution_quote_count: int
    source_policy_version: str


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
            "caution",
            "upstream source reports reliability concerns for Pinnacle fields "
            f"on or after {PINNACLE_CAUTION_CUTOFF.isoformat()}",
        )
    return "source-provided", None


def _aggregate_quality() -> tuple[str, str | None]:
    return "source-provided-aggregate", None


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

    teams_by_key: dict[str, TeamRecord] = {}
    display_by_key: dict[str, str] = {}
    games_by_key: dict[str, GameRecord] = {}
    exact_row_signatures: dict[tuple[tuple[str, str], ...], str] = {}
    duplicate_rows_discarded = 0
    warnings: list[str] = []
    odds_quotes: list[OddsQuoteRecord] = []
    statistics_rows: list[PostMatchStatisticsRecord] = []
    pinnacle_caution_quote_count = 0

    for index, row in enumerate(rows, start=2):  # header is row 1; data starts at 2
        signature = tuple(sorted(row.items()))
        source_game_preview = _preview_game_key(
            row=row,
            source_name=source_name,
            competition_id=competition_id,
            season_id=season_id,
        )
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
            if key not in teams_by_key:
                team_id = build_team_id(source_name=source_name, normalized_source_team_key=key)
                teams_by_key[key] = TeamRecord(
                    team_id=team_id,
                    sport_code=SPORT_FOOTBALL,
                    source_name=source_name,
                    source_team_key=key,
                    display_name=display,
                    normalized_name=key,
                    schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
                )

        event_date = parse_source_date(row.get("Date", ""))
        kickoff = parse_source_time(row.get("Time", ""))
        if kickoff is None:
            scheduled_start_utc = None
            start_time_precision = "date-only"
        else:
            scheduled_start_utc = combine_local_kickoff(
                event_date,
                kickoff,
                timezone_name=timezone_name,
            )
            start_time_precision = "minute"

        fthg = parse_optional_int(row.get("FTHG", ""), field_name="FTHG", maximum=MAX_GOALS)
        ftag = parse_optional_int(row.get("FTAG", ""), field_name="FTAG", maximum=MAX_GOALS)
        ftr_raw = row.get("FTR", "").strip()
        if (fthg is None) != (ftag is None):
            msg = f"row {index}: FTHG/FTAG must both be present or both empty"
            raise NormalizationError(msg)
        if fthg is None:
            status = "scheduled"
            full_time_result = None
            if ftr_raw:
                msg = f"row {index}: FTR must be empty for scheduled games"
                raise NormalizationError(msg)
        else:
            status = "finished"
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

        source_game_key = build_source_game_key(
            source_name=source_name,
            competition_id=competition_id,
            season_id=season_id,
            event_date=event_date.isoformat(),
            home_team_key=home_key,
            away_team_key=away_key,
        )
        if source_game_key in games_by_key:
            msg = f"row {index}: conflicting duplicate source game key"
            raise NormalizationError(msg)
        del source_game_preview

        game_id = build_game_id(source_game_key=source_game_key)
        game = GameRecord(
            game_id=game_id,
            sport_code=SPORT_FOOTBALL,
            competition_id=competition_id,
            season_id=season_id,
            source_name=source_name,
            source_game_key=source_game_key,
            source_row_number=index,
            event_date=event_date,
            scheduled_start_utc=scheduled_start_utc,
            start_time_precision=start_time_precision,
            status=status,
            home_team_id=teams_by_key[home_key].team_id,
            away_team_id=teams_by_key[away_key].team_id,
            full_time_home_goals=fthg,
            full_time_away_goals=ftag,
            full_time_result=full_time_result,
            half_time_home_goals=hthg,
            half_time_away_goals=htag,
            half_time_result=half_time_result,
            source_observed_at_utc=observed,
            source_file_sha256=source_file_sha256,
            schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        )
        games_by_key[source_game_key] = game
        exact_row_signatures[signature] = source_game_key

        # Odds triples
        for family in SUPPORTED_ODDS_FAMILIES:
            home_odds = parse_decimal_odds(
                row.get(family.home_column, ""),
                field_name=family.home_column,
            )
            draw_odds = parse_decimal_odds(
                row.get(family.draw_column, ""),
                field_name=family.draw_column,
            )
            away_odds = parse_decimal_odds(
                row.get(family.away_column, ""),
                field_name=family.away_column,
            )
            present = [value is not None for value in (home_odds, draw_odds, away_odds)]
            if not any(present):
                continue
            if not all(present):
                msg = (
                    f"row {index}: odds family {family.family_id} requires a complete H/D/A triple"
                )
                raise NormalizationError(msg)
            assert home_odds is not None and draw_odds is not None and away_odds is not None
            if family.provider_id == "pinnacle":
                quality_status, quality_reason = _pinnacle_quality(event_date)
                if quality_status == "caution":
                    pinnacle_caution_quote_count += 3
            elif family.provider_type.startswith("source-market"):
                quality_status, quality_reason = _aggregate_quality()
            else:
                quality_status, quality_reason = "source-provided", None
            for selection, odds_value, column in (
                ("home", home_odds, family.home_column),
                ("draw", draw_odds, family.draw_column),
                ("away", away_odds, family.away_column),
            ):
                quote_id = build_quote_id(
                    game_id=game_id,
                    market_type=MARKET_TYPE_1X2,
                    selection=selection,
                    provider_type=family.provider_type,
                    provider_id=family.provider_id,
                    quote_phase=family.quote_phase,
                    source_column_family=family.family_id,
                )
                odds_quotes.append(
                    OddsQuoteRecord(
                        quote_id=quote_id,
                        game_id=game_id,
                        market_type=MARKET_TYPE_1X2,
                        selection=selection,
                        provider_type=family.provider_type,
                        provider_id=family.provider_id,
                        quote_phase=family.quote_phase,
                        decimal_odds=odds_value,
                        source_column=column,
                        quoted_at_utc=None,
                        source_observed_at_utc=observed,
                        quote_timestamp_precision=QUOTE_TIMESTAMP_PRECISION,
                        source_file_sha256=source_file_sha256,
                        quality_status=quality_status,
                        quality_reason=quality_reason,
                        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
                    )
                )

        if status == "finished":
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
            if any(
                value is not None
                for value in (referee, hs, as_, hst, ast, hc, ac, hf, af, hy, ay, hr, ar)
            ):
                statistics_rows.append(
                    PostMatchStatisticsRecord(
                        game_id=game_id,
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
                        availability_stage=AVAILABILITY_POST_MATCH,
                        source_observed_at_utc=observed,
                        source_file_sha256=source_file_sha256,
                        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
                    )
                )

    if duplicate_rows_discarded:
        warnings.append(f"discarded_exact_duplicate_rows={duplicate_rows_discarded}")

    games = sorted(
        games_by_key.values(),
        key=lambda item: (
            item.event_date.toordinal(),
            # Null scheduled_start_utc sorts after non-null (documented policy).
            1 if item.scheduled_start_utc is None else 0,
            format_utc_timestamp(item.scheduled_start_utc)
            if item.scheduled_start_utc is not None
            else "",
            item.home_team_id,
            item.away_team_id,
            item.game_id,
        ),
    )
    teams = sorted(teams_by_key.values(), key=lambda item: item.team_id)
    odds_sorted = sorted(
        odds_quotes,
        key=lambda item: (
            item.game_id,
            item.quote_phase,
            item.provider_type,
            item.provider_id,
            item.selection,
            item.quote_id,
        ),
    )
    stats_sorted = sorted(statistics_rows, key=lambda item: item.game_id)

    return NormalizedFootballBundle(
        competitions=(competition,),
        seasons=(season,),
        teams=tuple(teams),
        games=tuple(games),
        odds_1x2=tuple(odds_sorted),
        post_match_statistics=tuple(stats_sorted),
        duplicate_rows_discarded=duplicate_rows_discarded,
        warnings=tuple(sorted(warnings)),
        pinnacle_caution_quote_count=pinnacle_caution_quote_count,
        source_policy_version=SOURCE_QUALITY_POLICY_VERSION,
    )


def _preview_game_key(
    *,
    row: dict[str, str],
    source_name: str,
    competition_id: str,
    season_id: str,
) -> str:
    """Best-effort preview used only before full validation (internal)."""
    try:
        home_key = normalize_team_name(row.get("HomeTeam", ""))[1]
        away_key = normalize_team_name(row.get("AwayTeam", ""))[1]
        event_date = parse_source_date(row.get("Date", "")).isoformat()
        return build_source_game_key(
            source_name=source_name,
            competition_id=competition_id,
            season_id=season_id,
            event_date=event_date,
            home_team_key=home_key,
            away_team_key=away_key,
        )
    except NormalizationError:
        return ""

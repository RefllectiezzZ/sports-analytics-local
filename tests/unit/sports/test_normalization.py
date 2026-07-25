"""Football normalization tests for canonical identity, markets, and statistics."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.core.exceptions import NormalizationError, SourceIntegrityError
from sports_analytics.markets.identifiers import (
    build_quote_observation_id,
    build_quote_series_id,
)
from sports_analytics.markets.schemas import quote_sort_key
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.contracts import IngestedEvent, IngestedSourceEvent
from sports_analytics.sports.football.identifiers import parse_canonical_season
from sports_analytics.sports.football.markets import (
    MARKET_KEY_MATCH_RESULT_1X2,
    SUPPORTED_ODDS_FAMILIES,
    match_result_1x2_selection,
)
from sports_analytics.sports.football.normalization import (
    PINNACLE_CAUTION_CUTOFF,
    NormalizedFootballBundle,
    PostMatchStatisticsRecord,
    combine_local_kickoff,
    normalize_football_rows,
    normalize_team_name,
    parse_source_date,
)
from sports_analytics.sports.reconciliation import RECONCILIATION_POLICY_VERSION

SOURCE_FILE_SHA256 = "c" * 64
OBSERVED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
ALTERNATE_SOURCE_NAME = "synthetic-second-source"


def _fixture_rows(name: str, *, expected_division_code: str) -> list[dict[str, str]]:
    content = (Path(__file__).parents[2] / "fixtures" / "football_data_co_uk" / name).read_bytes()
    parsed = parse_football_data_csv(content, expected_division_code=expected_division_code)
    return list(parsed.rows)


def _normalize(
    rows: list[dict[str, str]],
    *,
    competition_id: str = "eng-premier-league",
    season: str = "2023-2024",
    source_name: str = SOURCE_FOOTBALL_DATA_CO_UK,
) -> NormalizedFootballBundle:
    competition = get_competition(competition_id)
    label, start_year, end_year, source_season_code = parse_canonical_season(season)
    return normalize_football_rows(
        rows=rows,
        competition_id=competition.competition_id,
        competition_display_name=competition.display_name,
        country_code=competition.country_code,
        source_competition_code=competition.division_code,
        timezone_name=competition.timezone,
        season_label=label,
        start_year=start_year,
        end_year=end_year,
        source_season_code=source_season_code,
        source_name=source_name,
        source_file_sha256=SOURCE_FILE_SHA256,
        source_observed_at_utc=OBSERVED_AT,
    )


def _finished_row(**overrides: str) -> dict[str, str]:
    row = {
        "Div": "E0",
        "Date": "12/08/2023",
        "Time": "15:00",
        "HomeTeam": "Northbridge FC",
        "AwayTeam": "Southport Athletic",
        "FTHG": "2",
        "FTAG": "1",
        "FTR": "H",
        "HTHG": "1",
        "HTAG": "0",
        "HTR": "H",
        "Referee": "A Official",
        "HS": "10",
        "AS": "8",
        "HST": "5",
        "AST": "3",
        "HF": "12",
        "AF": "14",
        "HC": "5",
        "AC": "4",
        "HY": "2",
        "AY": "1",
        "HR": "0",
        "AR": "0",
    }
    row.update(overrides)
    return row


def _statistics_for(
    bundle: NormalizedFootballBundle,
    event: IngestedEvent,
) -> PostMatchStatisticsRecord:
    return next(
        item
        for item in bundle.post_match_statistics
        if item.canonical_event_id == event.canonical.canonical_event_id
    )


def _source_event_for(
    bundle: NormalizedFootballBundle,
    event: IngestedEvent,
) -> IngestedSourceEvent:
    return next(
        item
        for item in bundle.source_events
        if item.source_reference.canonical_event_id == event.canonical.canonical_event_id
    )


def test_normalize_synthetic_fixture_builds_canonical_bundle() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    assert bundle.competitions[0].competition_id == "eng-premier-league"
    assert bundle.competitions[0].source_competition_code == "E0"
    assert bundle.seasons[0].season_id == "eng-premier-league:2023-2024"
    assert bundle.seasons[0].source_season_code == "2324"
    assert {item.canonical.display_name for item in bundle.participants} == {
        "Northbridge FC",
        "Southport Athletic",
    }
    assert len(bundle.source_events) == 3
    assert len(bundle.events) == 1
    assert len(bundle.market_quotes) == 24
    assert len(bundle.post_match_statistics) == 1
    assert len(bundle.unresolved_reconciliations) == 2
    assert bundle.unresolved_event_count == 2
    assert bundle.duplicate_rows_discarded == 0
    assert bundle.warnings == ("unresolved_events=2",)
    assert bundle.pinnacle_caution_quote_count == 0
    assert bundle.source_policy_version == "football-data-co-uk-policy-v1"
    assert bundle.reconciliation_policy_version == RECONCILIATION_POLICY_VERSION
    assert bundle.participant_reconciliation_policy_version == "participant-reconciliation-v1"


def test_normalize_fixture_events_have_expected_statuses_and_times() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    assert [(event.event_date, event.status) for event in bundle.source_events] == [
        (date(2023, 8, 12), "finished"),
        (date(2023, 8, 12), "finished"),
        (date(2023, 8, 13), "scheduled"),
    ]
    assert [event.reconciliation.state for event in bundle.source_events] == [
        "unresolved",
        "exact",
        "unresolved",
    ]
    canonical = bundle.events[0].canonical
    assert canonical.status == "finished"
    assert canonical.result_code == "draw"
    assert (canonical.home_score, canonical.away_score) == (0, 0)
    assert canonical.event_date == date(2023, 8, 12)
    assert canonical.scheduled_start_utc == datetime(2023, 8, 12, 16, 30, tzinfo=UTC)
    assert canonical.start_time_precision == "minute"
    assert canonical.event_occurrence_key == "season-ordered-pair-home-1"


def test_normalize_fixture_records_source_row_provenance() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    references = [event.source_reference for event in bundle.source_events]
    # Header is row 1, so the first data row is row 2.
    assert [item.source_row_number for item in references] == [2, 3, 4]
    canonical_ids = {event.canonical.canonical_event_id for event in bundle.events}
    for reference in references:
        assert reference.source_name == SOURCE_FOOTBALL_DATA_CO_UK
        assert reference.source_event_key.startswith(f"{SOURCE_FOOTBALL_DATA_CO_UK}|")
        assert reference.source_observed_at_utc == OBSERVED_AT
        assert reference.source_file_sha256 == SOURCE_FILE_SHA256
        if reference.canonical_event_id is not None:
            assert reference.canonical_event_id in canonical_ids


def test_normalize_reconciles_single_source_events_exactly() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    for event in bundle.events:
        assert event.reconciliation.state == "exact"
        assert event.reconciliation.confidence == 1.0
        assert event.reconciliation.policy_version == RECONCILIATION_POLICY_VERSION
        assert event.reconciliation.canonical_event_id == event.canonical.canonical_event_id
        assert event.reconciliation.reason is None
        assert event.reconciliation.is_downstream_safe
    assert {item.reconciliation.state for item in bundle.source_events} == {"exact", "unresolved"}
    reconciliations = bundle.reconciliations
    assert len(reconciliations) == len(bundle.source_events)
    assert list(reconciliations) == sorted(
        reconciliations,
        key=lambda item: (item.source_name, item.source_event_id),
    )


def test_normalize_fixture_market_quotes_use_canonical_1x2_market() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    assert {quote.selection.outcome_key for quote in bundle.market_quotes} == {
        "home",
        "draw",
        "away",
    }
    for quote in bundle.market_quotes:
        definition = quote.selection.definition
        assert definition.market_key == MARKET_KEY_MATCH_RESULT_1X2
        assert definition.market_key == "football.match-result.1x2.full-match"
        assert definition.market_family == "match-result"
        assert definition.market_period == "full-match"
        assert definition.participant_scope == "event"
        assert definition.line_type == "none"
        assert definition.line_value is None
        assert definition.canonical_participant_id is None
        assert quote.quoted_at_utc is None
        assert quote.quote_timestamp_precision == "snapshot-observation-only"
        assert quote.source_observed_at_utc == OBSERVED_AT
    assert quote.source_name == SOURCE_FOOTBALL_DATA_CO_UK


def test_normalize_fixture_market_quote_is_decimal_and_deterministically_identified() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")
    bundle = _normalize(rows)
    first_event = bundle.events[0]

    quote = next(
        item
        for item in bundle.market_quotes
        if item.canonical_event_id == first_event.canonical.canonical_event_id
        and item.provider_id == "bet365"
        and item.quote_phase == "opening"
        and item.selection.outcome_key == "home"
    )

    source_event = _source_event_for(bundle, first_event)
    selection = match_result_1x2_selection("home")
    assert quote.decimal_odds == Decimal("2.4000")
    assert quote.source_field == "B365H"
    assert quote.source_event_id == source_event.source_reference.source_event_id
    assert quote.source_file_sha256 == SOURCE_FILE_SHA256
    assert quote.quote_series_id == build_quote_series_id(
        canonical_event_id=first_event.canonical.canonical_event_id,
        selection=selection,
        provider_type="bookmaker",
        provider_id="bet365",
    )
    assert quote.quote_observation_id == build_quote_observation_id(
        quote_series_id=quote.quote_series_id,
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        source_event_id=source_event.source_reference.source_event_id,
        selection=selection,
        provider_type="bookmaker",
        provider_id="bet365",
        quote_phase="opening",
        source_observed_at_utc=OBSERVED_AT,
        quoted_at_utc=None,
        source_file_sha256=SOURCE_FILE_SHA256,
        source_field="B365H",
    )
    uuid.UUID(quote.quote_series_id)
    uuid.UUID(quote.quote_observation_id)


def test_market_quotes_preserve_provider_phase_and_source_column() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")
    bundle = _normalize(rows)
    first_event_id = bundle.events[0].canonical.canonical_event_id

    home_quotes = {
        (quote.provider_id, quote.quote_phase): quote
        for quote in bundle.market_quotes
        if quote.canonical_event_id == first_event_id and quote.selection.outcome_key == "home"
    }

    assert set(home_quotes) == {
        (family.provider_id, family.quote_phase) for family in SUPPORTED_ODDS_FAMILIES
    }
    assert home_quotes[("bet365", "opening")].decimal_odds == Decimal("2.4000")
    assert home_quotes[("bet365", "closing")].decimal_odds == Decimal("2.5000")
    assert home_quotes[("pinnacle", "opening")].decimal_odds == Decimal("2.4500")
    assert home_quotes[("pinnacle", "closing")].decimal_odds == Decimal("2.5500")
    assert home_quotes[("market-average", "opening")].decimal_odds == Decimal("2.4200")
    assert home_quotes[("market-maximum", "closing")].decimal_odds == Decimal("2.6500")
    assert home_quotes[("bet365", "opening")].provider_type == "bookmaker"
    assert home_quotes[("market-average", "opening")].provider_type == "source-market-average"
    assert home_quotes[("market-maximum", "opening")].provider_type == "source-market-maximum"
    assert home_quotes[("market-average", "closing")].quality_status == "source-provided-aggregate"
    assert {quote.source_field for quote in home_quotes.values()} == {
        family.home_column for family in SUPPORTED_ODDS_FAMILIES
    }
    # Unsupported source columns are never ingested as quotes.
    assert all(quote.source_field != "WeirdBookH" for quote in bundle.market_quotes)


def test_opening_and_closing_quotes_are_separate_rows() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")
    bundle = _normalize(rows)
    first_event_id = bundle.events[0].canonical.canonical_event_id

    bet365_home = [
        quote
        for quote in bundle.market_quotes
        if quote.canonical_event_id == first_event_id
        and quote.provider_id == "bet365"
        and quote.selection.outcome_key == "home"
    ]

    assert [quote.quote_phase for quote in bet365_home] == ["closing", "opening"]
    assert len({quote.quote_series_id for quote in bet365_home}) == 1
    assert len({quote.quote_observation_id for quote in bet365_home}) == 2
    assert len({quote.decimal_odds for quote in bet365_home}) == 2


def test_market_quotes_are_deterministically_ordered() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    first = _normalize(rows)
    second = _normalize(list(rows))

    assert list(first.market_quotes) == sorted(first.market_quotes, key=quote_sort_key)
    assert [quote.quote_observation_id for quote in first.market_quotes] == [
        quote.quote_observation_id for quote in second.market_quotes
    ]


def test_repeated_normalization_is_byte_stable() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    first = _normalize(rows)
    second = _normalize(list(rows))

    assert first.events == second.events
    assert first.participants == second.participants
    assert first.market_quotes == second.market_quotes
    assert first.post_match_statistics == second.post_match_statistics
    assert first.reconciliations == second.reconciliations


def test_canonical_identity_is_independent_of_source_name() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    first = _normalize(rows)
    second = _normalize(rows, source_name=ALTERNATE_SOURCE_NAME)

    assert [event.canonical for event in first.events] == [
        event.canonical for event in second.events
    ]
    assert {item.canonical for item in first.participants} == {
        item.canonical for item in second.participants
    }
    assert [quote.quote_series_id for quote in first.market_quotes] == [
        quote.quote_series_id for quote in second.market_quotes
    ]
    assert [quote.quote_observation_id for quote in first.market_quotes] != [
        quote.quote_observation_id for quote in second.market_quotes
    ]


def test_source_scoped_identity_depends_on_source_name() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    first = _normalize(rows)
    second = _normalize(rows, source_name=ALTERNATE_SOURCE_NAME)

    first_event_ids = {event.source_reference.source_event_id for event in first.source_events}
    second_event_ids = {event.source_reference.source_event_id for event in second.source_events}
    assert first_event_ids.isdisjoint(second_event_ids)
    first_participant_ids = {
        item.source_reference.source_participant_id for item in first.participants
    }
    second_participant_ids = {
        item.source_reference.source_participant_id for item in second.participants
    }
    assert first_participant_ids.isdisjoint(second_participant_ids)
    assert all(
        item.source_reference.source_name == ALTERNATE_SOURCE_NAME for item in second.participants
    )
    assert all(
        event.source_reference.source_event_key.startswith(f"{ALTERNATE_SOURCE_NAME}|")
        for event in second.source_events
    )


def test_participant_identifiers_separate_canonical_and_source_scopes() -> None:
    bundle = _normalize([_finished_row()])

    for participant in bundle.participants:
        assert participant.canonical is not None
        canonical_id = participant.canonical.canonical_participant_id
        source_id = participant.source_reference.source_participant_id
        assert canonical_id != source_id
        assert participant.source_reference.canonical_participant_id == canonical_id
        assert participant.canonical.participant_type == "team"
        assert participant.canonical.canonical_key == participant.canonical.display_name.casefold()
        uuid.UUID(canonical_id)
        uuid.UUID(source_id)


def test_normalize_fixture_statistics_are_post_match_only() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    first_event = bundle.events[0]
    source_event = _source_event_for(bundle, first_event)
    stats = _statistics_for(bundle, first_event)
    assert stats.source_event_id == source_event.source_reference.source_event_id
    assert stats.availability_stage == "post-match"
    assert stats.referee == "B Official"
    assert stats.home_shots == 7
    assert stats.away_shots == 9
    assert stats.home_shots_on_target == 2
    assert stats.away_shots_on_target == 4
    assert stats.home_corners == 3
    assert stats.away_corners == 6
    assert stats.home_fouls == 11
    assert stats.away_fouls == 10
    assert stats.home_yellow_cards == 1
    assert stats.away_yellow_cards == 2
    assert stats.home_red_cards == 0
    assert stats.away_red_cards == 0


def test_half_time_goals_live_in_post_match_statistics_only() -> None:
    rows = _fixture_rows("epl_2023_2024_synthetic.csv", expected_division_code="E0")

    bundle = _normalize(rows)

    stats = _statistics_for(bundle, bundle.events[0])
    assert (stats.half_time_home_goals, stats.half_time_away_goals) == (0, 0)
    assert stats.half_time_result == "draw"
    canonical = bundle.events[0].canonical
    assert not hasattr(canonical, "half_time_home_goals")
    assert not hasattr(canonical, "half_time_result")


def test_normalize_accepts_minimal_primeira_liga_fixture_without_statistics() -> None:
    rows = _fixture_rows("prt_2023_2024_synthetic.csv", expected_division_code="P1")

    bundle = _normalize(rows, competition_id="prt-primeira-liga")

    assert bundle.competitions[0].competition_id == "prt-primeira-liga"
    assert len(bundle.events) == 2
    assert len(bundle.market_quotes) == 6
    assert bundle.post_match_statistics == ()


def test_normalize_discards_exact_duplicate_rows() -> None:
    row = _finished_row()

    bundle = _normalize([row, dict(row)])

    assert len(bundle.events) == 1
    assert bundle.duplicate_rows_discarded == 1
    assert bundle.warnings == ("discarded_exact_duplicate_rows=1",)


def test_conflicting_duplicate_source_rows_are_rejected() -> None:
    """One source file describing the same fixture twice is a source-integrity error.

    Cross-source ambiguity is handled by the reconciler (unresolved); a conflicting
    duplicate inside a single file is rejected loudly instead of silently dropping
    the fixture from the snapshot.
    """
    with pytest.raises(SourceIntegrityError, match="conflicting duplicate source event identity"):
        _normalize([_finished_row(), _finished_row(FTHG="3", FTAG="1")])


def test_normalize_requires_timezone_aware_observation_time() -> None:
    competition = get_competition("eng-premier-league")
    label, start_year, end_year, source_season_code = parse_canonical_season("2023-2024")

    with pytest.raises(NormalizationError, match="timezone-aware"):
        normalize_football_rows(
            rows=[_finished_row()],
            competition_id=competition.competition_id,
            competition_display_name=competition.display_name,
            country_code=competition.country_code,
            source_competition_code=competition.division_code,
            timezone_name=competition.timezone,
            season_label=label,
            start_year=start_year,
            end_year=end_year,
            source_season_code=source_season_code,
            source_name=SOURCE_FOOTBALL_DATA_CO_UK,
            source_file_sha256=SOURCE_FILE_SHA256,
            source_observed_at_utc=datetime(2025, 1, 2, 3, 4, 5),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"FTHG": "1", "FTAG": "0", "FTR": "A"}, "FTR inconsistent"),
        ({"FTHG": "", "FTAG": "", "FTR": "H"}, "FTR must be empty"),
        ({"FTHG": "2", "FTAG": "", "FTR": "H"}, "FTHG/FTAG must both be present"),
        ({"FTHG": "2", "FTAG": "1", "FTR": ""}, "FTR is required"),
        ({"HTHG": "1", "HTAG": "0", "HTR": "A"}, "HTR inconsistent"),
        ({"HTHG": "", "HTAG": "", "HTR": "H"}, "HTR must be empty"),
        ({"HTHG": "1", "HTAG": "0", "HTR": ""}, "HTR is required"),
        ({"HomeTeam": "Northbridge FC", "AwayTeam": "Northbridge\tFC"}, "must differ"),
        ({"Date": "2023-08-12"}, "Date must use"),
        ({"Date": ""}, "Date is required"),
        ({"Time": "25:00"}, "Time must use"),
    ],
)
def test_normalize_rejects_invalid_event_rows(
    overrides: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(NormalizationError, match=message):
        _normalize([_finished_row(**overrides)])


def test_normalize_rejects_team_name_normalization_collision() -> None:
    rows = [
        _finished_row(),
        _finished_row(Date="19/08/2023", HomeTeam="NORTHBRIDGE FC"),
    ]

    with pytest.raises(NormalizationError, match="team normalization collision"):
        _normalize(rows)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Northbridge   FC ", ("Northbridge FC", "northbridge fc")),
        ("Northbridge\tFC", ("Northbridge FC", "northbridge fc")),
        ("Cafe\u0301 Rovers", ("Caf\u00e9 Rovers", "caf\u00e9 rovers")),
    ],
)
def test_normalize_team_name_collapses_whitespace_and_composes_unicode(
    raw: str,
    expected: tuple[str, str],
) -> None:
    assert normalize_team_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("Northbridge\nFC", "control characters"),
        ("Northbridge\x00FC", "NUL"),
        ("   ", "non-empty after normalization"),
        ("N" * 129, "exceeds maximum length"),
    ],
)
def test_normalize_team_name_rejects_unsafe_values(raw: str, message: str) -> None:
    with pytest.raises(NormalizationError, match=message):
        normalize_team_name(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12/08/2023", date(2023, 8, 12)),
        ("12/08/23", date(2023, 8, 12)),
    ],
)
def test_parse_source_date_accepts_documented_formats(raw: str, expected: date) -> None:
    assert parse_source_date(raw) == expected


def test_combine_local_kickoff_prefers_earlier_ambiguous_local_time() -> None:
    # 01:30 local occurs twice on the UK DST fall-back day; policy takes fold=0.
    combined = combine_local_kickoff(
        date(2024, 10, 27),
        time(1, 30),
        timezone_name="Europe/London",
    )

    assert combined == datetime(2024, 10, 27, 0, 30, tzinfo=UTC)


def test_combine_local_kickoff_rejects_nonexistent_local_time() -> None:
    with pytest.raises(NormalizationError, match="does not exist"):
        combine_local_kickoff(date(2024, 3, 31), time(1, 30), timezone_name="Europe/London")


def test_combine_local_kickoff_rejects_unknown_timezone() -> None:
    with pytest.raises(NormalizationError, match="invalid competition timezone"):
        combine_local_kickoff(date(2024, 5, 1), time(15, 0), timezone_name="Not/AZone")


def test_normalize_rejects_partial_odds_triples() -> None:
    row = _finished_row(B365H="1.80", B365D="", B365A="4.50")

    with pytest.raises(NormalizationError, match="requires a complete H/D/A triple"):
        _normalize([row])


@pytest.mark.parametrize("bad_odds", ["true", "NaN", "1.00", "1,80"])
def test_normalize_rejects_invalid_decimal_odds(bad_odds: str) -> None:
    row = _finished_row(B365H=bad_odds, B365D="3.50", B365A="4.50")

    with pytest.raises(NormalizationError):
        _normalize([row])


def test_normalize_parses_odds_as_exact_decimals() -> None:
    row = _finished_row(B365H="1.83", B365D="3.55", B365A="4.45")

    bundle = _normalize([row])

    odds = {
        quote.selection.outcome_key: quote.decimal_odds
        for quote in bundle.market_quotes
        if quote.provider_id == "bet365" and quote.quote_phase == "opening"
    }
    assert odds == {
        "home": Decimal("1.8300"),
        "draw": Decimal("3.5500"),
        "away": Decimal("4.4500"),
    }


@pytest.mark.parametrize(
    ("event_date", "expected_status", "expected_count"),
    [
        ("22/07/2025", "source-provided", 0),
        ("23/07/2025", "caution", 3),
    ],
)
def test_pinnacle_quote_quality_changes_at_documented_cutoff(
    event_date: str,
    expected_status: str,
    expected_count: int,
) -> None:
    row = _finished_row(Date=event_date, PSH="1.80", PSD="3.50", PSA="4.50")

    bundle = _normalize([row], season="2025-2026")

    pinnacle_quotes = [quote for quote in bundle.market_quotes if quote.provider_id == "pinnacle"]
    assert len(pinnacle_quotes) == 3
    assert {quote.quality_status for quote in pinnacle_quotes} == {expected_status}
    assert bundle.pinnacle_caution_quote_count == expected_count
    if expected_status == "caution":
        assert all(
            str(PINNACLE_CAUTION_CUTOFF) in (quote.quality_reason or "")
            for quote in pinnacle_quotes
        )
    else:
        assert all(quote.quality_reason is None for quote in pinnacle_quotes)


def test_statistics_referee_is_normalized() -> None:
    row = _finished_row(Referee="  A   Official  ")

    bundle = _normalize([row])

    assert bundle.post_match_statistics[0].referee == "A Official"


def test_statistics_reject_partial_home_away_pairs() -> None:
    row = _finished_row(HS="10", AS="")

    with pytest.raises(NormalizationError, match="HS/AS must both be present"):
        _normalize([row])

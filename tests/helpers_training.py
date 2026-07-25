"""Helpers for synthetic football training fixtures."""

from __future__ import annotations

from datetime import date, timedelta

from sports_analytics.features.football.prematch import FinishedTrainingEvent
from sports_analytics.sports.identifiers import (
    FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY,
    build_canonical_event_id,
    build_canonical_participant_id,
)


def make_club_id(name: str, *, scope: str = "club:england") -> str:
    """Build a deterministic canonical club id for tests."""
    return build_canonical_participant_id(
        sport_code="football",
        participant_type="club",
        participant_identity_scope=scope,
        canonical_key=name.casefold(),
    )


CLUBS = (
    "Northbridge FC",
    "Southport Athletic",
    "Eastmere United",
    "Westfield Town",
    "Riverdale Rovers",
    "Hilltop Wanderers",
)


def synthetic_finished_events(
    *,
    competition_id: str = "eng-premier-league",
    season_ids: tuple[str, ...] = ("eng-premier-league:2022-2023", "eng-premier-league:2023-2024"),
    start: date = date(2022, 8, 1),
    matches_per_season: int = 60,
) -> tuple[FinishedTrainingEvent, ...]:
    """Create a compact multi-season finished-event history for local tests."""
    club_ids = [make_club_id(name) for name in CLUBS]
    events: list[FinishedTrainingEvent] = []
    current = start
    results = ("home", "draw", "away")
    for season_index, season_id in enumerate(season_ids):
        for match_index in range(matches_per_season):
            home = club_ids[match_index % len(club_ids)]
            away = club_ids[(match_index + 1 + season_index) % len(club_ids)]
            if home == away:
                away = club_ids[(match_index + 2) % len(club_ids)]
            result = results[match_index % 3]
            if result == "home":
                home_score, away_score = 2, 1
            elif result == "draw":
                home_score, away_score = 1, 1
            else:
                home_score, away_score = 0, 2
            event_id = build_canonical_event_id(
                sport_code="football",
                competition_id=competition_id,
                season_id=season_id,
                home_canonical_participant_id=home,
                away_canonical_participant_id=away,
                event_occurrence_key=f"{FOOTBALL_DOMESTIC_HOME_OCCURRENCE_KEY}-{match_index}",
            )
            # Two matches on some dates to exercise same-date isolation.
            if match_index % 7 == 0 and match_index > 0:
                event_date = current
            else:
                current = current + timedelta(days=1)
                event_date = current
            events.append(
                FinishedTrainingEvent(
                    canonical_event_id=event_id,
                    sport_code="football",
                    competition_id=competition_id,
                    season_id=season_id,
                    event_date=event_date,
                    scheduled_start_utc=None,
                    home_canonical_participant_id=home,
                    away_canonical_participant_id=away,
                    home_score=home_score,
                    away_score=away_score,
                    result_code=result,
                )
            )
        current = current + timedelta(days=30)
    return tuple(events)


def synthetic_season_csv(
    *,
    division_code: str = "E0",
    season_start_year: int = 2023,
    match_count: int = 30,
    include_closing_avg: bool = True,
) -> bytes:
    """Build a Football-Data-like CSV with unique home/away pairs for one season.

    Football-Data domestic-league occurrence keys allow one home meeting per
    ordered pair per season, so this helper never repeats a home/away pair.
    """
    header = [
        "Div",
        "Date",
        "HomeTeam",
        "AwayTeam",
        "FTHG",
        "FTAG",
        "FTR",
    ]
    if include_closing_avg:
        header.extend(["AvgCH", "AvgCD", "AvgCA"])
    lines = [",".join(header)]
    clubs = list(CLUBS)
    pairs: list[tuple[str, str]] = [
        (home, away) for home in clubs for away in clubs if home != away
    ]
    if match_count > len(pairs):
        msg = f"match_count={match_count} exceeds unique ordered pairs={len(pairs)}"
        raise ValueError(msg)
    results = ["H", "D", "A"]
    day = date(season_start_year, 8, 5)
    for index, (home, away) in enumerate(pairs[:match_count]):
        result = results[index % 3]
        if result == "H":
            scores = "2,1,H"
        elif result == "D":
            scores = "1,1,D"
        else:
            scores = "0,1,A"
        if index % 5 == 0 and index > 0:
            match_day = day
        else:
            day = day + timedelta(days=1)
            match_day = day
        row = f"{division_code},{match_day.strftime('%d/%m/%Y')},{home},{away},{scores}"
        if include_closing_avg:
            row += f",{1.8 + (index % 5) * 0.1:.2f},{3.40:.2f},{4.2 - (index % 4) * 0.1:.2f}"
        lines.append(row)
    return ("\n".join(lines) + "\n").encode("utf-8")

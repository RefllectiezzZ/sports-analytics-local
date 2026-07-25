"""Leakage-safe football pre-match feature generation (team-level v1).

Conservative calendar-date batching
-----------------------------------
1. Order finished events by ``event_date``, then ``canonical_event_id``.
2. Generate features for every event on a date from team state available
   **before** that date.
3. Only after all features for the date are generated, update team state with
   that date's finished results.

Same-date matches therefore cannot influence one another. Changing a future
event or result never alters an earlier feature row.

Player, lineup, and injury features are intentionally out of scope for v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from sports_analytics.core.exceptions import FeatureError
from sports_analytics.features.contracts import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
    FeatureRowMetadata,
    football_1x2_prematch_specification,
)
from sports_analytics.sports.contracts import EventStatus
from sports_analytics.sports.football.markets import MATCH_RESULT_1X2_OUTCOMES

ELO_CONFIG_VERSION: Final[str] = "football-elo-v1"
ELO_INITIAL_RATING: Final[float] = 1500.0
ELO_K_FACTOR: Final[float] = 20.0
ELO_HOME_ADVANTAGE: Final[float] = 65.0
#: Season transition policy: carry Elo forward across seasons without reset.
ELO_SEASON_TRANSITION_POLICY: Final[str] = "carry-forward-no-reset"

COLD_START_REST_DAYS: Final[float] = 7.0
ROLLING_WINDOWS: Final[tuple[int, ...]] = (5, 10)


@dataclass(frozen=True, slots=True)
class FinishedTrainingEvent:
    """One finished canonical event eligible for feature generation."""

    canonical_event_id: str
    sport_code: str
    competition_id: str
    season_id: str
    event_date: date
    scheduled_start_utc: datetime | None
    home_canonical_participant_id: str
    away_canonical_participant_id: str
    home_score: int
    away_score: int
    result_code: str


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """One leakage-safe feature row plus its target label and metadata."""

    metadata: FeatureRowMetadata
    features: dict[str, float]
    result_code: str

    def ordered_values(self) -> tuple[float, ...]:
        """Return feature values in the specification whitelist order."""
        return tuple(self.features[name] for name in FOOTBALL_1X2_FEATURE_NAMES_V1)


@dataclass(frozen=True, slots=True)
class EloConfiguration:
    """Fixed, versioned Elo policy used by football-1x2-prematch-features-v1."""

    version: str = ELO_CONFIG_VERSION
    initial_rating: float = ELO_INITIAL_RATING
    k_factor: float = ELO_K_FACTOR
    home_advantage: float = ELO_HOME_ADVANTAGE
    season_transition_policy: str = ELO_SEASON_TRANSITION_POLICY


@dataclass
class _MatchHistoryEntry:
    event_date: date
    is_home: bool
    goals_for: int
    goals_against: int
    points: int


@dataclass
class _TeamState:
    elo: float
    history: list[_MatchHistoryEntry] = field(default_factory=list)
    last_match_date: date | None = None


def validate_training_events(
    events: tuple[FinishedTrainingEvent, ...],
    *,
    expected_competition_id: str | None = None,
) -> tuple[FinishedTrainingEvent, ...]:
    """Reject mixed sports/competitions, incomplete targets, and duplicates."""
    if not events:
        msg = "training requires at least one finished canonical event"
        raise FeatureError(msg)

    competition_ids = {item.competition_id for item in events}
    sport_codes = {item.sport_code for item in events}
    if len(sport_codes) != 1:
        msg = f"mixed sports are not allowed in one feature artifact: {sorted(sport_codes)}"
        raise FeatureError(msg)
    sport_code = next(iter(sport_codes))
    if sport_code != "football":
        msg = f"football feature builder received unsupported sport: {sport_code}"
        raise FeatureError(msg)
    if len(competition_ids) != 1:
        msg = (
            "mixed competitions are not allowed in one model/feature artifact: "
            f"{sorted(competition_ids)}"
        )
        raise FeatureError(msg)
    competition_id = next(iter(competition_ids))
    if expected_competition_id is not None and competition_id != expected_competition_id:
        msg = f"competition mismatch: expected {expected_competition_id}, got {competition_id}"
        raise FeatureError(msg)

    seen: dict[str, FinishedTrainingEvent] = {}
    for event in events:
        if event.result_code not in MATCH_RESULT_1X2_OUTCOMES:
            msg = (
                f"event {event.canonical_event_id} has incomplete or invalid "
                f"result_code={event.result_code!r}"
            )
            raise FeatureError(msg)
        if event.home_score < 0 or event.away_score < 0:
            msg = f"event {event.canonical_event_id} has invalid scores"
            raise FeatureError(msg)
        previous = seen.get(event.canonical_event_id)
        if previous is not None:
            if previous != event:
                msg = f"duplicate conflicting canonical events: {event.canonical_event_id}"
                raise FeatureError(msg)
            continue
        seen[event.canonical_event_id] = event

    return tuple(
        sorted(
            seen.values(),
            key=lambda item: (item.event_date.isoformat(), item.canonical_event_id),
        )
    )


def generate_prematch_features(
    events: tuple[FinishedTrainingEvent, ...],
    *,
    elo_config: EloConfiguration | None = None,
) -> tuple[FeatureVector, ...]:
    """Generate leakage-safe team-level features with daily batching."""
    specification = football_1x2_prematch_specification()
    del specification  # specification pins the whitelist used below
    ordered = validate_training_events(events)
    config = elo_config or EloConfiguration()
    states: dict[str, _TeamState] = {}
    vectors: list[FeatureVector] = []

    index = 0
    while index < len(ordered):
        current_date = ordered[index].event_date
        batch: list[FinishedTrainingEvent] = []
        while index < len(ordered) and ordered[index].event_date == current_date:
            batch.append(ordered[index])
            index += 1

        for event in batch:
            home_state = states.setdefault(
                event.home_canonical_participant_id,
                _TeamState(elo=config.initial_rating),
            )
            away_state = states.setdefault(
                event.away_canonical_participant_id,
                _TeamState(elo=config.initial_rating),
            )
            features = _build_features(
                home_state=home_state,
                away_state=away_state,
                event_date=event.event_date,
                config=config,
            )
            metadata = FeatureRowMetadata(
                canonical_event_id=event.canonical_event_id,
                competition_id=event.competition_id,
                season_id=event.season_id,
                event_date=event.event_date,
                scheduled_start_utc=event.scheduled_start_utc,
                feature_cutoff_date=event.event_date,
                feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
                home_canonical_participant_id=event.home_canonical_participant_id,
                away_canonical_participant_id=event.away_canonical_participant_id,
            )
            vectors.append(
                FeatureVector(
                    metadata=metadata,
                    features=features,
                    result_code=event.result_code,
                )
            )

        for event in batch:
            _apply_result(
                states=states,
                event=event,
                config=config,
            )

    return tuple(vectors)


def expected_home_score(home_elo: float, away_elo: float, *, home_advantage: float) -> float:
    """Return the Elo expected score for the home side including home advantage."""
    home_rating = home_elo + home_advantage
    return float(1.0 / (1.0 + 10.0 ** ((away_elo - home_rating) / 400.0)))


def _build_features(
    *,
    home_state: _TeamState,
    away_state: _TeamState,
    event_date: date,
    config: EloConfiguration,
) -> dict[str, float]:
    home_elo = home_state.elo
    away_elo = away_state.elo
    home_days, home_rest_available = _days_since_previous(home_state.last_match_date, event_date)
    away_days, away_rest_available = _days_since_previous(away_state.last_match_date, event_date)

    home_ppg_5, home_w5 = _rolling_mean_points(home_state.history, 5)
    home_ppg_10, home_w10 = _rolling_mean_points(home_state.history, 10)
    away_ppg_5, away_w5 = _rolling_mean_points(away_state.history, 5)
    away_ppg_10, away_w10 = _rolling_mean_points(away_state.history, 10)

    home_gf_5, _ = _rolling_mean_stat(home_state.history, 5, "goals_for")
    home_gf_10, _ = _rolling_mean_stat(home_state.history, 10, "goals_for")
    away_gf_5, _ = _rolling_mean_stat(away_state.history, 5, "goals_for")
    away_gf_10, _ = _rolling_mean_stat(away_state.history, 10, "goals_for")

    home_ga_5, _ = _rolling_mean_stat(home_state.history, 5, "goals_against")
    home_ga_10, _ = _rolling_mean_stat(home_state.history, 10, "goals_against")
    away_ga_5, _ = _rolling_mean_stat(away_state.history, 5, "goals_against")
    away_ga_10, _ = _rolling_mean_stat(away_state.history, 10, "goals_against")

    home_gd_5, _ = _rolling_mean_goal_diff(home_state.history, 5)
    home_gd_10, _ = _rolling_mean_goal_diff(home_state.history, 10)
    away_gd_5, _ = _rolling_mean_goal_diff(away_state.history, 5)
    away_gd_10, _ = _rolling_mean_goal_diff(away_state.history, 10)

    home_home_ppg_5, home_home_available = _rolling_mean_points(
        [item for item in home_state.history if item.is_home],
        5,
    )
    away_away_ppg_5, away_away_available = _rolling_mean_points(
        [item for item in away_state.history if not item.is_home],
        5,
    )

    features = {
        "home_elo": home_elo,
        "away_elo": away_elo,
        "elo_diff": home_elo - away_elo,
        "home_matches_played": float(len(home_state.history)),
        "away_matches_played": float(len(away_state.history)),
        "home_ppg_5": home_ppg_5,
        "home_ppg_10": home_ppg_10,
        "away_ppg_5": away_ppg_5,
        "away_ppg_10": away_ppg_10,
        "home_gf_pm_5": home_gf_5,
        "home_gf_pm_10": home_gf_10,
        "away_gf_pm_5": away_gf_5,
        "away_gf_pm_10": away_gf_10,
        "home_ga_pm_5": home_ga_5,
        "home_ga_pm_10": home_ga_10,
        "away_ga_pm_5": away_ga_5,
        "away_ga_pm_10": away_ga_10,
        "home_gd_pm_5": home_gd_5,
        "home_gd_pm_10": home_gd_10,
        "away_gd_pm_5": away_gd_5,
        "away_gd_pm_10": away_gd_10,
        "home_home_ppg_5": home_home_ppg_5,
        "away_away_ppg_5": away_away_ppg_5,
        "home_days_since_prev": home_days,
        "away_days_since_prev": away_days,
        "rest_day_diff": home_days - away_days,
        "home_window5_available": home_w5,
        "home_window10_available": home_w10,
        "away_window5_available": away_w5,
        "away_window10_available": away_w10,
        "home_home_form_available": home_home_available,
        "away_away_form_available": away_away_available,
        "home_rest_available": home_rest_available,
        "away_rest_available": away_rest_available,
    }
    if tuple(features) != FOOTBALL_1X2_FEATURE_NAMES_V1:
        msg = "internal feature name order drifted from the v1 whitelist"
        raise FeatureError(msg)
    return features


def _apply_result(
    *,
    states: dict[str, _TeamState],
    event: FinishedTrainingEvent,
    config: EloConfiguration,
) -> None:
    home_state = states[event.home_canonical_participant_id]
    away_state = states[event.away_canonical_participant_id]
    expected_home = expected_home_score(
        home_state.elo,
        away_state.elo,
        home_advantage=config.home_advantage,
    )
    if event.result_code == "home":
        actual_home = 1.0
        home_points = 3
        away_points = 0
    elif event.result_code == "draw":
        actual_home = 0.5
        home_points = 1
        away_points = 1
    elif event.result_code == "away":
        actual_home = 0.0
        home_points = 0
        away_points = 3
    else:
        msg = f"unsupported result_code for Elo update: {event.result_code}"
        raise FeatureError(msg)

    home_state.elo = home_state.elo + config.k_factor * (actual_home - expected_home)
    away_state.elo = away_state.elo + config.k_factor * (
        (1.0 - actual_home) - (1.0 - expected_home)
    )
    home_state.history.append(
        _MatchHistoryEntry(
            event_date=event.event_date,
            is_home=True,
            goals_for=event.home_score,
            goals_against=event.away_score,
            points=home_points,
        )
    )
    away_state.history.append(
        _MatchHistoryEntry(
            event_date=event.event_date,
            is_home=False,
            goals_for=event.away_score,
            goals_against=event.home_score,
            points=away_points,
        )
    )
    home_state.last_match_date = event.event_date
    away_state.last_match_date = event.event_date


def _days_since_previous(last_match_date: date | None, event_date: date) -> tuple[float, float]:
    if last_match_date is None:
        return COLD_START_REST_DAYS, 0.0
    delta = (event_date - last_match_date).days
    if delta < 0:
        msg = "team history contains a match after the current event date"
        raise FeatureError(msg)
    return float(delta), 1.0


def _rolling_mean_points(history: list[_MatchHistoryEntry], window: int) -> tuple[float, float]:
    if not history:
        return 0.0, 0.0
    sample = history[-window:]
    mean = sum(item.points for item in sample) / float(len(sample))
    available = 1.0 if len(sample) >= min(window, 1) and len(history) > 0 else 0.0
    # Availability is 1 when at least one prior match exists for the window request.
    available = 1.0 if sample else 0.0
    return mean, available


def _rolling_mean_stat(
    history: list[_MatchHistoryEntry],
    window: int,
    attr: str,
) -> tuple[float, float]:
    if not history:
        return 0.0, 0.0
    sample = history[-window:]
    total = sum(getattr(item, attr) for item in sample)
    return total / float(len(sample)), 1.0


def _rolling_mean_goal_diff(
    history: list[_MatchHistoryEntry],
    window: int,
) -> tuple[float, float]:
    if not history:
        return 0.0, 0.0
    sample = history[-window:]
    total = sum(item.goals_for - item.goals_against for item in sample)
    return total / float(len(sample)), 1.0


def training_event_from_row(row: dict[str, object]) -> FinishedTrainingEvent:
    """Build a finished training event from a canonical events Parquet row."""
    status = str(row["status"])
    if status != EventStatus.FINISHED.value:
        msg = "only finished canonical events may be used as training rows"
        raise FeatureError(msg)
    result_code = row.get("result_code")
    home_score = row.get("home_score")
    away_score = row.get("away_score")
    if result_code is None or home_score is None or away_score is None:
        msg = "finished training rows require scores and result_code"
        raise FeatureError(msg)
    if not isinstance(home_score, int | float) or not isinstance(away_score, int | float):
        msg = "finished training rows require numeric scores"
        raise FeatureError(msg)
    event_date_value = row["event_date"]
    if hasattr(event_date_value, "as_py"):
        event_date_value = event_date_value.as_py()
    if isinstance(event_date_value, datetime):
        event_date_value = event_date_value.date()
    if not isinstance(event_date_value, date):
        msg = f"invalid event_date type: {type(event_date_value)!r}"
        raise FeatureError(msg)
    scheduled = row.get("scheduled_start_utc")
    if hasattr(scheduled, "as_py"):
        scheduled = scheduled.as_py()
    if scheduled is not None and not isinstance(scheduled, datetime):
        msg = "scheduled_start_utc must be datetime or null"
        raise FeatureError(msg)
    return FinishedTrainingEvent(
        canonical_event_id=str(row["canonical_event_id"]),
        sport_code=str(row["sport_code"]),
        competition_id=str(row["competition_id"]),
        season_id=str(row["season_id"]),
        event_date=event_date_value,
        scheduled_start_utc=scheduled,
        home_canonical_participant_id=str(row["home_canonical_participant_id"]),
        away_canonical_participant_id=str(row["away_canonical_participant_id"]),
        home_score=int(home_score),
        away_score=int(away_score),
        result_code=str(result_code),
    )

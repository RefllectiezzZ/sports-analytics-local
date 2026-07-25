"""Football-specific feature row metadata extensions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sports_analytics.features.contracts import FeatureRowMetadata

FOOTBALL_FORBIDDEN_MODEL_FEATURE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "home_score",
        "away_score",
        "HTHG",
        "HTAG",
        "HTR",
        "AvgH",
        "AvgD",
        "AvgA",
        "AvgCH",
        "AvgCD",
        "AvgCA",
        "B365H",
        "B365D",
        "B365A",
        "B365CH",
        "B365CD",
        "B365CA",
    }
)

FOOTBALL_SCOPE_COMPETITION_ID: Final[str] = "competition_id"
FOOTBALL_SCOPE_SEASON_ID: Final[str] = "season_id"
FOOTBALL_SCOPE_HOME_PARTICIPANT_ID: Final[str] = "home_canonical_participant_id"
FOOTBALL_SCOPE_AWAY_PARTICIPANT_ID: Final[str] = "away_canonical_participant_id"


@dataclass(frozen=True, slots=True)
class FootballFeatureRowMetadata:
    """Football 1X2 feature metadata with competition and participant context."""

    base: FeatureRowMetadata
    competition_id: str
    season_id: str
    home_canonical_participant_id: str
    away_canonical_participant_id: str

    @classmethod
    def create(
        cls,
        *,
        canonical_event_id: str,
        competition_id: str,
        season_id: str,
        event_date: date,
        scheduled_start_utc: datetime | None,
        feature_cutoff_date: date,
        feature_specification_version: str,
        home_canonical_participant_id: str,
        away_canonical_participant_id: str,
    ) -> FootballFeatureRowMetadata:
        """Build football metadata with a generic base scope payload."""
        base = FeatureRowMetadata(
            canonical_event_id=canonical_event_id,
            event_date=event_date,
            scheduled_start_utc=scheduled_start_utc,
            feature_cutoff_date=feature_cutoff_date,
            feature_specification_version=feature_specification_version,
            scope_metadata={
                FOOTBALL_SCOPE_COMPETITION_ID: competition_id,
                FOOTBALL_SCOPE_SEASON_ID: season_id,
                FOOTBALL_SCOPE_HOME_PARTICIPANT_ID: home_canonical_participant_id,
                FOOTBALL_SCOPE_AWAY_PARTICIPANT_ID: away_canonical_participant_id,
            },
        )
        return cls(
            base=base,
            competition_id=competition_id,
            season_id=season_id,
            home_canonical_participant_id=home_canonical_participant_id,
            away_canonical_participant_id=away_canonical_participant_id,
        )

    @property
    def canonical_event_id(self) -> str:
        return self.base.canonical_event_id

    @property
    def event_date(self) -> date:
        return self.base.event_date

    @property
    def scheduled_start_utc(self) -> datetime | None:
        return self.base.scheduled_start_utc

    @property
    def feature_cutoff_date(self) -> date:
        return self.base.feature_cutoff_date

    @property
    def feature_specification_version(self) -> str:
        return self.base.feature_specification_version

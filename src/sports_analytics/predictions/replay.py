"""Conservative historical replay timing derived from persisted event starts."""

from __future__ import annotations

from datetime import datetime, timedelta

from sports_analytics.sports.contracts import require_utc

HISTORICAL_REPLAY_CUTOFF_DELTA = timedelta(microseconds=1)


def derive_historical_replay_cutoff_utc(event_start_utc: datetime) -> datetime:
    """Derive the single safe replay cutoff immediately before a stored event start."""
    start = require_utc(event_start_utc, field_name="event_start_utc")
    return start - HISTORICAL_REPLAY_CUTOFF_DELTA

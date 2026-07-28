"""Strict immutable acquisition-window policy for bookmaker inventory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sports_analytics.core.exceptions import (
    PermanentJobError,
    PermanentSourceError,
    RepositoryError,
)
from sports_analytics.data.codec import (
    dumps_canonical_json,
    format_utc_timestamp,
    parse_utc_timestamp,
)
from sports_analytics.data.types import JsonValue, validate_identifier, validate_strict_int
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sports.contracts import require_utc

ACQUISITION_WINDOW_POLICY_V1: Final[str] = "bookmaker-acquisition-window-v1"
ALL_OBSERVED_MARKETS: Final[str] = "all-observed-markets"
DEFAULT_WINDOW_HOURS: Final[int] = 48
DEFAULT_MAXIMUM_WINDOW_HOURS: Final[int] = 168
DEFAULT_MAXIMUM_EVENTS: Final[int] = 100
HARD_MAXIMUM_EVENTS: Final[int] = 500

_WINDOW_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "window_start_utc",
        "window_end_utc",
        "maximum_events",
        "requested_market_depth",
        "include_live",
        "evaluated_at_utc",
        "policy_id",
    }
)


@dataclass(frozen=True, slots=True)
class AcquisitionWindow:
    """A deterministic, bounded UTC event horizon."""

    window_start_utc: datetime
    window_end_utc: datetime
    maximum_events: int
    requested_market_depth: str = ALL_OBSERVED_MARKETS
    include_live: bool = False
    evaluated_at_utc: datetime | None = None
    policy_id: str = ACQUISITION_WINDOW_POLICY_V1
    maximum_window_hours: int = DEFAULT_MAXIMUM_WINDOW_HOURS

    def __post_init__(self) -> None:
        start = require_utc(self.window_start_utc, field_name="window_start_utc")
        end = require_utc(self.window_end_utc, field_name="window_end_utc")
        evaluated = self.evaluated_at_utc
        if evaluated is not None:
            evaluated = require_utc(evaluated, field_name="evaluated_at_utc")
        maximum_events = validate_strict_int(
            self.maximum_events,
            field_name="maximum_events",
            minimum=1,
            maximum=HARD_MAXIMUM_EVENTS,
        )
        maximum_hours = validate_strict_int(
            self.maximum_window_hours,
            field_name="maximum_window_hours",
            minimum=1,
            maximum=DEFAULT_MAXIMUM_WINDOW_HOURS,
        )
        if end <= start:
            msg = "window_end_utc must be strictly after window_start_utc"
            raise PermanentSourceError(msg)
        if end - start > timedelta(hours=maximum_hours):
            msg = f"acquisition window exceeds maximum horizon of {maximum_hours} hours"
            raise PermanentSourceError(msg)
        if self.requested_market_depth != ALL_OBSERVED_MARKETS:
            msg = f"requested_market_depth must be {ALL_OBSERVED_MARKETS}"
            raise PermanentSourceError(msg)
        if type(self.include_live) is not bool:
            msg = "include_live must be a boolean"
            raise PermanentSourceError(msg)
        if self.include_live:
            msg = "live bookmaker acquisition is disabled by policy"
            raise PermanentSourceError(msg)
        validate_identifier(self.policy_id, field_name="policy_id")
        object.__setattr__(self, "window_start_utc", start)
        object.__setattr__(self, "window_end_utc", end)
        object.__setattr__(self, "evaluated_at_utc", evaluated)
        object.__setattr__(self, "maximum_events", maximum_events)
        object.__setattr__(self, "maximum_window_hours", maximum_hours)

    def contains(self, scheduled_start_utc: datetime) -> bool:
        """Return whether an event starts in the half-open window."""
        scheduled = require_utc(scheduled_start_utc, field_name="scheduled_start_utc")
        return self.window_start_utc <= scheduled < self.window_end_utc

    def as_payload(self) -> dict[str, JsonValue]:
        """Return the exact deterministic job-payload representation."""
        return {
            "window_start_utc": format_utc_timestamp(self.window_start_utc),
            "window_end_utc": format_utc_timestamp(self.window_end_utc),
            "maximum_events": self.maximum_events,
            "requested_market_depth": self.requested_market_depth,
            "include_live": self.include_live,
            "evaluated_at_utc": (
                None
                if self.evaluated_at_utc is None
                else format_utc_timestamp(self.evaluated_at_utc)
            ),
            "policy_id": self.policy_id,
        }

    @property
    def identity_digest(self) -> str:
        """Return a stable digest for idempotency and cycle identity."""
        encoded = dumps_canonical_json(self.as_payload()).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        maximum_window_hours: int = DEFAULT_MAXIMUM_WINDOW_HOURS,
    ) -> AcquisitionWindow:
        """Parse an exact acquisition-window payload and reject unknown fields."""
        if not isinstance(payload, dict):
            msg = "acquisition_window must be an object"
            raise PermanentJobError(msg)
        unknown = sorted(set(payload) - _WINDOW_PAYLOAD_KEYS)
        missing = sorted(
            {
                "window_start_utc",
                "window_end_utc",
                "maximum_events",
                "requested_market_depth",
                "include_live",
                "policy_id",
            }
            - set(payload)
        )
        if unknown:
            msg = f"unknown acquisition_window fields: {', '.join(unknown)}"
            raise PermanentJobError(msg)
        if missing:
            msg = f"missing acquisition_window fields: {', '.join(missing)}"
            raise PermanentJobError(msg)
        try:
            start_raw = payload["window_start_utc"]
            end_raw = payload["window_end_utc"]
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                raise ValueError("window timestamps must be strings")
            evaluated_raw = payload.get("evaluated_at_utc")
            if evaluated_raw is not None and not isinstance(evaluated_raw, str):
                raise ValueError("evaluated_at_utc must be a string or null")
            depth = payload["requested_market_depth"]
            policy = payload["policy_id"]
            if not isinstance(depth, str) or not isinstance(policy, str):
                raise ValueError("market depth and policy ID must be strings")
            include_live = payload["include_live"]
            if type(include_live) is not bool:
                raise ValueError("include_live must be a boolean")
            return cls(
                window_start_utc=parse_utc_timestamp(start_raw),
                window_end_utc=parse_utc_timestamp(end_raw),
                maximum_events=validate_strict_int(
                    payload["maximum_events"],
                    field_name="maximum_events",
                    minimum=1,
                    maximum=HARD_MAXIMUM_EVENTS,
                ),
                requested_market_depth=depth,
                include_live=include_live,
                evaluated_at_utc=(
                    None if evaluated_raw is None else parse_utc_timestamp(evaluated_raw)
                ),
                policy_id=policy,
                maximum_window_hours=maximum_window_hours,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            PermanentSourceError,
            RepositoryError,
        ) as exc:
            raise PermanentJobError(str(exc)) from exc


def rolling_acquisition_window(
    evaluated_at_utc: datetime,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    maximum_events: int = DEFAULT_MAXIMUM_EVENTS,
    maximum_window_hours: int = DEFAULT_MAXIMUM_WINDOW_HOURS,
) -> AcquisitionWindow:
    """Build the default rolling horizon from an actual evaluation time."""
    evaluated = require_utc(evaluated_at_utc, field_name="evaluated_at_utc").astimezone(UTC)
    hours = validate_strict_int(
        window_hours,
        field_name="window_hours",
        minimum=1,
        maximum=maximum_window_hours,
    )
    return AcquisitionWindow(
        window_start_utc=evaluated,
        window_end_utc=evaluated + timedelta(hours=hours),
        maximum_events=maximum_events,
        evaluated_at_utc=evaluated,
        maximum_window_hours=maximum_window_hours,
    )


def apply_acquisition_window(
    bundle: ProviderAcquisitionBundle,
    acquisition_window: AcquisitionWindow,
) -> ProviderAcquisitionBundle:
    """Return a provider bundle with deterministic pre-match window admission.

    The loose input annotation avoids a dependency cycle at module import time;
    runtime validation remains exact.
    """
    from dataclasses import replace

    from sports_analytics.sources.bookmaker_contracts import ProviderEventState

    if not isinstance(bundle, ProviderAcquisitionBundle):
        msg = "acquisition window requires a ProviderAcquisitionBundle"
        raise PermanentSourceError(msg)
    admitted = tuple(
        sorted(
            (
                event
                for event in bundle.events
                if event.event_state is ProviderEventState.PRE_MATCH
                and acquisition_window.contains(event.scheduled_start_utc)
            ),
            key=lambda event: (event.scheduled_start_utc, event.source_event_id),
        )
    )[: acquisition_window.maximum_events]
    excluded = len(bundle.events) - len(admitted)
    drift_codes = bundle.drift_codes
    if excluded:
        drift_codes = tuple(sorted(set((*drift_codes, "event-outside-window"))))
    return replace(bundle, events=admitted, drift_codes=drift_codes)

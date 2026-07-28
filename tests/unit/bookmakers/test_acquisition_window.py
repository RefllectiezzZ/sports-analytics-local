"""Strict acquisition-window and deterministic inventory admission tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.bookmakers.window import (
    AcquisitionWindow,
    apply_acquisition_window,
)
from sports_analytics.core.exceptions import PermanentJobError, PermanentSourceError
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderParticipantObservation,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _event(source_event_id: str, start: datetime) -> ProviderEventObservation:
    return ProviderEventObservation(
        source_event_id=source_event_id,
        source_competition_id="competition-1",
        sport="football",
        scheduled_start_utc=start,
        event_state=ProviderEventState.PRE_MATCH,
        participants=(
            ProviderParticipantObservation("home-1", "Synthetic Home", "home"),
            ProviderParticipantObservation("away-1", "Synthetic Away", "away"),
        ),
        markets=(),
        source_page_route_id="football-prematch",
    )


def test_window_payload_round_trip_and_identity_are_deterministic() -> None:
    window = AcquisitionWindow(
        window_start_utc=NOW,
        window_end_utc=NOW + timedelta(hours=48),
        maximum_events=25,
        evaluated_at_utc=NOW,
    )
    loaded = AcquisitionWindow.from_payload(window.as_payload())
    assert loaded.as_payload() == window.as_payload()
    assert loaded.identity_digest == window.identity_digest


@pytest.mark.parametrize(
    "payload_update",
    [
        {"include_live": 1},
        {"maximum_events": True},
        {"window_start_utc": "2026-07-28T12:00:00Z"},
        {"extra": "forbidden"},
    ],
)
def test_window_payload_is_exact_and_strict(payload_update: dict[str, object]) -> None:
    window = AcquisitionWindow(
        window_start_utc=NOW,
        window_end_utc=NOW + timedelta(hours=2),
        maximum_events=2,
    )
    payload: dict[str, object] = dict(window.as_payload())
    payload.update(payload_update)
    with pytest.raises(PermanentJobError):
        AcquisitionWindow.from_payload(payload)


def test_window_rejects_invalid_range_and_excessive_horizon() -> None:
    with pytest.raises(PermanentSourceError, match="strictly after"):
        AcquisitionWindow(
            window_start_utc=NOW,
            window_end_utc=NOW,
            maximum_events=1,
        )
    with pytest.raises(PermanentSourceError, match="maximum horizon"):
        AcquisitionWindow(
            window_start_utc=NOW,
            window_end_utc=NOW + timedelta(hours=169),
            maximum_events=1,
        )


def test_window_filter_is_ordered_half_open_and_bounded() -> None:
    window = AcquisitionWindow(
        window_start_utc=NOW,
        window_end_utc=NOW + timedelta(hours=4),
        maximum_events=2,
    )
    bundle = ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="adapter-v1",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=NOW,
        sport="football",
        events=(
            _event("late", NOW + timedelta(hours=3)),
            _event("outside-end", NOW + timedelta(hours=4)),
            _event("early", NOW + timedelta(hours=1)),
            _event("middle", NOW + timedelta(hours=2)),
        ),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )
    admitted = apply_acquisition_window(bundle, window)
    assert [event.source_event_id for event in admitted.events] == ["early", "middle"]
    assert admitted.drift_codes == ("event-outside-window",)

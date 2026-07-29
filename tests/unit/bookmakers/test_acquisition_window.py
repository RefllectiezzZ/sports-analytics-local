"""Strict acquisition-window and deterministic inventory admission tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.bookmakers.window import (
    AcquisitionWindow,
    apply_acquisition_window,
    apply_acquisition_window_with_counts,
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


def _non_prematch_event(
    source_event_id: str,
    start: datetime,
) -> ProviderEventObservation:
    event = _event(source_event_id, start)
    return ProviderEventObservation(
        source_event_id=event.source_event_id,
        source_competition_id=event.source_competition_id,
        sport=event.sport,
        scheduled_start_utc=event.scheduled_start_utc,
        event_state=ProviderEventState.UNKNOWN,
        participants=event.participants,
        markets=event.markets,
        source_page_route_id=event.source_page_route_id,
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
    application = apply_acquisition_window_with_counts(bundle, window)
    assert [event.source_event_id for event in application.bundle.events] == [
        "early",
        "middle",
    ]
    assert application.bundle.drift_codes == (
        "event-at-or-after-window",
        "event-limit-truncated",
    )
    assert application.counts.at_or_after_window_end_excluded == 1
    assert application.counts.eligible_events == 3
    assert application.counts.admitted_events == 2
    assert application.counts.event_limit_truncated == 1
    assert all(
        event.completeness.completeness_state.value == "partial-event-limit"
        for event in application.bundle.events
    )


def test_outside_window_filtering_does_not_imply_event_limit_truncation() -> None:
    window = AcquisitionWindow(
        window_start_utc=NOW,
        window_end_utc=NOW + timedelta(hours=4),
        maximum_events=3,
    )
    bundle = ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="adapter-v1",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=NOW,
        sport="football",
        events=(
            _event("before", NOW - timedelta(seconds=1)),
            _event("at-start", NOW),
            _event("at-end", NOW + timedelta(hours=4)),
            _non_prematch_event("unknown-state", NOW + timedelta(hours=1)),
        ),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )
    application = apply_acquisition_window_with_counts(bundle, window)
    assert [event.source_event_id for event in application.bundle.events] == ["at-start"]
    assert application.bundle.drift_codes == (
        "event-at-or-after-window",
        "event-before-window",
        "non-prematch-event-excluded",
    )
    assert application.counts.non_pre_match_excluded == 1
    assert application.counts.before_window_excluded == 1
    assert application.counts.at_or_after_window_end_excluded == 1
    assert application.counts.event_limit_truncated == 0


def test_event_limit_boundary_is_deterministic_for_equal_start_times() -> None:
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
        events=tuple(
            _event(source_event_id, NOW + timedelta(hours=1))
            for source_event_id in ("event-c", "event-a", "event-b")
        ),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )
    admitted = apply_acquisition_window(bundle, window)
    assert [event.source_event_id for event in admitted.events] == [
        "event-a",
        "event-b",
    ]
    assert admitted.drift_codes == ("event-limit-truncated",)

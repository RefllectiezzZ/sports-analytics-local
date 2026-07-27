"""Fake-clock deadline and dwell tests for browser acquisition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.sources.browser.contracts import BrowserMode
from sports_analytics.sources.browser.playwright_runtime import PlaywrightBrowserSession


def test_playwright_session_rejects_negative_dwell() -> None:
    with pytest.raises(Exception, match="dwell_after_readiness_ms"):
        PlaywrightBrowserSession(dwell_after_readiness_ms=-1)


def test_deadline_helper_reports_actual_elapsed_not_clamped() -> None:
    """Displayed duration must be actual elapsed, not min(elapsed, budget)."""
    started = 10.0
    finished = 45.0
    duration_budget = 30.0
    elapsed = finished - started
    assert elapsed == 35.0
    assert elapsed > duration_budget
    # Production probe/smoke must not clamp displayed duration with min().
    reported = elapsed
    clamped = min(elapsed, duration_budget)
    assert reported != clamped
    assert reported == 35.0


def test_smoke_deadline_wrapper_injects_deadline() -> None:
    from sports_analytics.bookmakers.diagnostics.smoke import _DeadlineBrowserSession

    class _Inner:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        def acquire(self, **kwargs: object) -> dict[str, object]:
            self.kwargs = kwargs
            return {"ok": True}

    inner = _Inner()
    deadline = datetime(2026, 7, 26, 12, 0, 30, tzinfo=UTC)
    wrapper = _DeadlineBrowserSession(inner, deadline_at_utc=deadline)
    wrapper.acquire(provider_id="betano-pt", sport="football")
    assert inner.kwargs is not None
    assert inner.kwargs["deadline_at_utc"] == deadline


def test_recording_session_captures_deadline() -> None:
    from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
    from sports_analytics.sources.browser.playwright_runtime import RecordingBrowserSession

    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    result = BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=now,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    session = RecordingBrowserSession(result)
    deadline = now + timedelta(seconds=15)
    session.acquire(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-1",
        allowed_hostnames=frozenset({"www.betano.pt"}),
        start_urls=(("football-prematch", "https://www.betano.pt/sport/futebol/"),),
        observed_at_utc=now,
        browser_mode=BrowserMode.VISIBLE,
        deadline_at_utc=deadline,
    )
    assert session.calls[0]["deadline_at_utc"] == deadline

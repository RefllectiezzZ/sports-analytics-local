"""Network metadata observation and observation-window tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.browser.playwright_runtime import (
    PlaywrightBrowserSession,
    RecordingBrowserSession,
    build_network_metadata,
    detect_candidate_keys,
    sanitized_path_hash,
)
from sports_analytics.sources.browser.readiness import wait_for_readiness
from sports_analytics.sources.browser.safety import classify_https_public_url

ALLOWED = frozenset({"www.betano.pt"})
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_metadata_only_for_unknown_public_host() -> None:
    meta = build_network_metadata(
        response_url="https://cdn.example.com/api/v1/track?x=1",
        allowed_hostnames=ALLOWED,
        status_code=200,
        content_type="application/json",
        resource_type="xhr",
        observed_at_utc=NOW,
        body_text='{"events":[{"id":"1"}]}',
        byte_size=100,
    )
    assert meta is not None
    assert meta.hostname == "cdn.example.com"
    assert meta.hostname_approved is False
    assert meta.body_captured is False
    assert meta.structural_fingerprint is None
    assert meta.candidate_keys_detected is False
    assert meta.sanitized_path_hash == sanitized_path_hash(
        "https://cdn.example.com/api/v1/track?x=1"
    )
    # Path-only hash must ignore query string.
    assert meta.sanitized_path_hash == sanitized_path_hash("https://cdn.example.com/api/v1/track")


def test_approved_json_captures_body_and_candidate_keys() -> None:
    body = '{"events":[{"markets":[{"odds":1.5}]}]}'
    meta = build_network_metadata(
        response_url="https://www.betano.pt/api/events",
        allowed_hostnames=ALLOWED,
        status_code=200,
        content_type="application/json",
        resource_type="fetch",
        observed_at_utc=NOW,
        body_text=body,
    )
    assert meta is not None
    assert meta.hostname_approved is True
    assert meta.body_captured is True
    assert meta.candidate_keys_detected is True
    assert meta.structural_fingerprint is not None
    assert meta.byte_size == len(body.encode("utf-8"))


def test_private_host_rejected_for_metadata() -> None:
    assert classify_https_public_url("https://127.0.0.1/api") is None
    assert classify_https_public_url("https://localhost/api") is None
    assert classify_https_public_url("https://10.0.0.8/api") is None
    assert classify_https_public_url("http://cdn.example.com/api") is None
    assert classify_https_public_url("https://user:pass@cdn.example.com/api") is None

    meta = build_network_metadata(
        response_url="https://192.168.1.10/secret",
        allowed_hostnames=ALLOWED | {"192.168.1.10"},
        status_code=200,
        content_type="application/json",
        resource_type="xhr",
        observed_at_utc=NOW,
        body_text="{}",
    )
    assert meta is None


def test_classify_https_public_url_accepts_public_host() -> None:
    assert classify_https_public_url("https://cdn.example.com/x") == "cdn.example.com"


def test_detect_candidate_keys() -> None:
    assert detect_candidate_keys({"events": []}) is True
    assert detect_candidate_keys({"data": {"markets": {}}}) is True
    assert detect_candidate_keys({"version": 1, "ok": True}) is False


def test_recording_session_captures_observation_dwell_kwargs() -> None:
    result = BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )
    session = RecordingBrowserSession(result)
    deadline = NOW + timedelta(seconds=30)
    complete = lambda: False  # noqa: E731
    session.acquire(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-1",
        allowed_hostnames=ALLOWED,
        start_urls=(("football-prematch", "https://www.betano.pt/sport/futebol/"),),
        observed_at_utc=NOW,
        browser_mode=BrowserMode.VISIBLE,
        deadline_at_utc=deadline,
        observation_window_ms=30_000,
        observation_complete=complete,
    )
    assert session.calls[0]["deadline_at_utc"] == deadline
    assert session.calls[0]["observation_window_ms"] == 30_000
    assert session.calls[0]["observation_complete"] is complete


def test_advancing_clock_bounds_readiness_timeout() -> None:
    """Readiness timeout must use remaining deadline, never a fixed 15s when less remains."""

    class _Clock:
        def __init__(self) -> None:
            self.now = NOW

        def __call__(self) -> datetime:
            return self.now

        def advance(self, ms: int) -> None:
            self.now = self.now + timedelta(milliseconds=ms)

    clock = _Clock()
    deadline = NOW + timedelta(seconds=2)
    remaining_ms = max(1, int((deadline - clock()).total_seconds() * 1000))
    assert remaining_ms == 2000
    assert remaining_ms < 15_000

    class FakePage:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def title(self) -> str:
            return "Loading"

        def inner_text(self, selector: str) -> str:
            del selector
            return ""

        def wait_for_timeout(self, timeout: float) -> None:
            self.waits.append(timeout)
            clock.advance(int(timeout))

    page = FakePage()
    with pytest.raises(Exception, match="readiness timeout"):
        wait_for_readiness(
            page,
            predicate=lambda _: False,
            timeout_ms=remaining_ms,
            poll_ms=250,
            page_route_id="football-prematch",
        )
    assert sum(page.waits) >= 2000
    assert sum(page.waits) < 15_000


def test_observation_window_polls_until_complete() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait_for_timeout(self, timeout: float) -> None:
            self.waits.append(timeout)
            clock.advance(int(timeout))

    class _Clock:
        def __init__(self) -> None:
            self.now = NOW

        def __call__(self) -> datetime:
            return self.now

        def advance(self, ms: int) -> None:
            self.now = self.now + timedelta(milliseconds=ms)

    clock = _Clock()
    session = PlaywrightBrowserSession(clock=clock, dwell_after_readiness_ms=1_500)
    page = FakePage()
    flags = {"done": False}

    def complete() -> bool:
        return flags["done"]

    # Complete after first poll slice.
    original_wait = page.wait_for_timeout

    def wait_and_complete(timeout: float) -> None:
        original_wait(timeout)
        flags["done"] = True

    page.wait_for_timeout = wait_and_complete  # type: ignore[method-assign]
    session._observe_after_readiness(
        page,
        deadline=NOW + timedelta(seconds=30),
        observation_window_ms=10_000,
        observation_complete=complete,
        network_metadata=[],
    )
    assert len(page.waits) == 1


def test_observation_window_none_uses_short_dwell() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait_for_timeout(self, timeout: float) -> None:
            self.waits.append(timeout)

    session = PlaywrightBrowserSession(
        dwell_after_readiness_ms=1_500,
        clock=lambda: NOW,
    )
    page = FakePage()
    session._observe_after_readiness(
        page,
        deadline=NOW + timedelta(seconds=30),
        observation_window_ms=None,
        observation_complete=None,
        network_metadata=[],
    )
    assert page.waits == [1_500]

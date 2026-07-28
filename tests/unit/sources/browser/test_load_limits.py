"""Deterministic event-detail load-control tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.core.exceptions import PermanentSourceError, RetryableSourceError
from sports_analytics.sources.browser.limits import (
    BrowserAcquisitionLimits,
    DeterministicNavigationGate,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def test_default_policy_is_single_concurrency_fixed_interval_and_no_retry() -> None:
    limits = BrowserAcquisitionLimits()
    assert limits.event_detail_concurrency == 1
    assert limits.minimum_event_detail_interval_ms == 1_000
    assert limits.explicit_retry_limit == 0
    assert limits.maximum_response_bytes == 2_097_152
    assert limits.maximum_total_capture_bytes == 16_777_216


def test_navigation_gate_enforces_concurrency_and_fixed_interval() -> None:
    gate = DeterministicNavigationGate(BrowserAcquisitionLimits())
    gate.acquire(started_at_utc=NOW)
    with pytest.raises(RetryableSourceError, match="concurrency"):
        gate.acquire(started_at_utc=NOW + timedelta(seconds=2))
    gate.release()
    with pytest.raises(RetryableSourceError, match="interval"):
        gate.acquire(started_at_utc=NOW + timedelta(milliseconds=999))
    gate.acquire(started_at_utc=NOW + timedelta(seconds=1))
    gate.release()


def test_load_policy_rejects_booleans_and_invalid_total_budget() -> None:
    with pytest.raises(PermanentSourceError, match="booleans"):
        BrowserAcquisitionLimits(event_detail_concurrency=True)  # type: ignore[arg-type]
    with pytest.raises(PermanentSourceError, match="cover"):
        BrowserAcquisitionLimits(
            maximum_response_bytes=2_048,
            maximum_total_capture_bytes=1_024,
        )

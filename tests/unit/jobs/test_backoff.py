"""Tests for deterministic retry backoff helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_analytics.core.exceptions import WorkerError
from sports_analytics.jobs.backoff import compute_retry_available_at, compute_retry_delay_seconds

FIXED = datetime(2026, 7, 24, 19, 30, 0, tzinfo=UTC)


def test_retry_delay_exponential_and_capped() -> None:
    assert compute_retry_delay_seconds(attempts=1, base_seconds=5, max_seconds=300) == 5
    assert compute_retry_delay_seconds(attempts=2, base_seconds=5, max_seconds=300) == 10
    assert compute_retry_delay_seconds(attempts=7, base_seconds=5, max_seconds=300) == 300
    assert compute_retry_delay_seconds(attempts=2000, base_seconds=5, max_seconds=300) == 300


def test_retry_available_at_uses_timezone_aware_failed_at() -> None:
    assert compute_retry_available_at(
        failed_at=FIXED,
        attempts=3,
        base_seconds=2.5,
        max_seconds=20,
    ) == FIXED + timedelta(seconds=10)
    with pytest.raises(WorkerError, match="failed_at"):
        compute_retry_available_at(
            failed_at=datetime(2026, 7, 24, 19, 30, 0),
            attempts=1,
            base_seconds=5,
            max_seconds=300,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"attempts": 0, "base_seconds": 5, "max_seconds": 300}, "attempts"),
        ({"attempts": True, "base_seconds": 5, "max_seconds": 300}, "attempts"),
        ({"attempts": 1, "base_seconds": 0, "max_seconds": 300}, "base_seconds"),
        ({"attempts": 1, "base_seconds": 5, "max_seconds": 4}, "max_seconds"),
        ({"attempts": 1, "base_seconds": float("inf"), "max_seconds": 300}, "base_seconds"),
    ],
)
def test_retry_delay_validation(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(WorkerError, match=match):
        compute_retry_delay_seconds(**kwargs)  # type: ignore[arg-type]

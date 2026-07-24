"""Deterministic exponential retry backoff without jitter."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sports_analytics.core.exceptions import WorkerError


def compute_retry_delay_seconds(
    *,
    attempts: int,
    base_seconds: float,
    max_seconds: float,
) -> float:
    """Return deterministic exponential backoff delay in seconds.

    Formula::

        delay = min(max_seconds, base_seconds * 2 ** (attempts - 1))

    ``attempts`` is the attempt count after the job was claimed. The first failed
    attempt therefore uses the base delay.
    """
    if type(attempts) is not int or isinstance(attempts, bool):
        msg = "attempts must be a positive int"
        raise WorkerError(msg)
    if attempts < 1:
        msg = "attempts must be >= 1"
        raise WorkerError(msg)
    for name, value in (
        ("base_seconds", base_seconds),
        ("max_seconds", max_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = f"{name} must be a positive finite number"
            raise WorkerError(msg)
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            msg = f"{name} must be a positive finite number"
            raise WorkerError(msg)
    if float(max_seconds) < float(base_seconds):
        msg = "max_seconds must be greater than or equal to base_seconds"
        raise WorkerError(msg)

    exponent = attempts - 1
    # Cap the exponent to avoid float overflow before applying min().
    if exponent >= 1023:
        return float(max_seconds)
    delay = float(base_seconds) * (2.0**exponent)
    if not math.isfinite(delay):
        return float(max_seconds)
    return min(float(max_seconds), delay)


def compute_retry_available_at(
    *,
    failed_at: datetime,
    attempts: int,
    base_seconds: float,
    max_seconds: float,
) -> datetime:
    """Return ``failed_at`` plus the deterministic retry delay."""
    if failed_at.tzinfo is None:
        msg = "failed_at must be timezone-aware"
        raise WorkerError(msg)
    delay = compute_retry_delay_seconds(
        attempts=attempts,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
    )
    return failed_at + timedelta(seconds=delay)

"""Deterministic exponential retry backoff without jitter."""

from __future__ import annotations

from datetime import datetime, timedelta

from sports_analytics.core.exceptions import RepositoryError, WorkerError
from sports_analytics.data.types import validate_positive_duration_seconds


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
    try:
        base = validate_positive_duration_seconds(base_seconds, field_name="base_seconds")
        maximum = validate_positive_duration_seconds(max_seconds, field_name="max_seconds")
    except RepositoryError as exc:
        raise WorkerError(str(exc)) from exc
    if maximum < base:
        msg = "max_seconds must be greater than or equal to base_seconds"
        raise WorkerError(msg)

    exponent = attempts - 1
    # Cap the exponent to avoid float overflow before applying min().
    if exponent >= 1023:
        return maximum
    delay = base * (2.0**exponent)
    if delay != delay or delay == float("inf"):  # NaN or inf guard
        return maximum
    return min(maximum, delay)


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
    try:
        return failed_at + timedelta(seconds=delay)
    except OverflowError as exc:
        msg = "retry available_at overflow for the computed delay"
        raise RepositoryError(msg) from exc

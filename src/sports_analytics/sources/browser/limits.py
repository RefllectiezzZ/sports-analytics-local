"""Conservative deterministic load policy for browser-observed acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sports_analytics.core.exceptions import PermanentSourceError, RetryableSourceError
from sports_analytics.sports.contracts import require_utc


@dataclass(frozen=True, slots=True)
class BrowserAcquisitionLimits:
    """Fixed bounds shared by inventory and future event-detail traversal."""

    maximum_response_bytes: int = 2_097_152
    maximum_total_capture_bytes: int = 16_777_216
    event_detail_concurrency: int = 1
    minimum_event_detail_interval_ms: int = 1_000
    navigation_timeout_ms: int = 30_000
    explicit_retry_limit: int = 0

    def __post_init__(self) -> None:
        integer_fields = (
            self.maximum_response_bytes,
            self.maximum_total_capture_bytes,
            self.event_detail_concurrency,
            self.minimum_event_detail_interval_ms,
            self.navigation_timeout_ms,
            self.explicit_retry_limit,
        )
        if any(isinstance(value, bool) for value in integer_fields):
            msg = "browser acquisition limits reject booleans"
            raise PermanentSourceError(msg)
        if self.maximum_response_bytes < 1:
            msg = "maximum_response_bytes must be positive"
            raise PermanentSourceError(msg)
        if self.maximum_total_capture_bytes < self.maximum_response_bytes:
            msg = "maximum_total_capture_bytes must cover at least one response"
            raise PermanentSourceError(msg)
        if self.event_detail_concurrency < 1 or self.event_detail_concurrency > 4:
            msg = "event_detail_concurrency must be between one and four"
            raise PermanentSourceError(msg)
        if self.minimum_event_detail_interval_ms < 0:
            msg = "minimum_event_detail_interval_ms must be non-negative"
            raise PermanentSourceError(msg)
        if self.navigation_timeout_ms < 1 or self.navigation_timeout_ms > 120_000:
            msg = "navigation_timeout_ms is outside the fixed safety bound"
            raise PermanentSourceError(msg)
        if self.explicit_retry_limit != 0:
            msg = "browser acquisition does not retry responses or explicit blocks"
            raise PermanentSourceError(msg)


class DeterministicNavigationGate:
    """Small injectable gate enforcing concurrency and fixed start intervals."""

    def __init__(self, limits: BrowserAcquisitionLimits) -> None:
        self._limits = limits
        self._active = 0
        self._last_started_at: datetime | None = None

    def acquire(self, *, started_at_utc: datetime) -> None:
        """Admit a navigation or fail without randomized waiting."""
        started = require_utc(started_at_utc, field_name="started_at_utc")
        if self._active >= self._limits.event_detail_concurrency:
            msg = "event-detail concurrency limit reached"
            raise RetryableSourceError(msg)
        if self._last_started_at is not None:
            earliest = self._last_started_at + timedelta(
                milliseconds=self._limits.minimum_event_detail_interval_ms
            )
            if started < earliest:
                msg = "event-detail minimum navigation interval not reached"
                raise RetryableSourceError(msg)
        self._active += 1
        self._last_started_at = started

    def release(self) -> None:
        """Release one active navigation deterministically."""
        if self._active < 1:
            msg = "event-detail navigation gate release is unbalanced"
            raise PermanentSourceError(msg)
        self._active -= 1

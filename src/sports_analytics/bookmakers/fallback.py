"""Explicit preferred/fallback provider resolution for bookmaker acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sports_analytics.bookmakers.types import (
    BOOKMAKER_FALLBACK_POLICY_ID,
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    FailureClassification,
    QuoteSelectionReason,
)
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sports.contracts import require_utc

#: Typed extension point for a later permitted fixture/odds source. Empty by
#: default so no third-party provider is enabled in PR #11.
DEFAULT_ADDITIONAL_FALLBACK_PROVIDERS: Final[tuple[str, ...]] = ()


@dataclass(frozen=True, slots=True)
class ProviderAttemptOutcome:
    """Outcome of one provider acquisition attempt in a fallback chain."""

    provider_id: str
    success: bool
    failure_classification: FailureClassification
    block_or_failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.success and self.failure_classification is not FailureClassification.NONE:
            msg = "successful provider attempt must use failure classification none"
            raise PermanentSourceError(msg)
        if not self.success and self.failure_classification is FailureClassification.NONE:
            msg = "failed provider attempt requires a non-none failure classification"
            raise PermanentSourceError(msg)


@dataclass(frozen=True, slots=True)
class CachedSnapshotReference:
    """Reference to a previously published valid snapshot that is not current."""

    snapshot_id: str
    observed_at_utc: datetime
    age_seconds: int
    is_current: bool = False

    def __post_init__(self) -> None:
        if self.is_current:
            msg = "cached snapshot reference must never be labelled current"
            raise PermanentSourceError(msg)
        if self.age_seconds < 0:
            msg = "cached snapshot age_seconds must be non-negative"
            raise PermanentSourceError(msg)
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )


@dataclass(frozen=True, slots=True)
class ProviderFallbackDecision:
    """Auditable result of preferred/fallback provider resolution."""

    preferred_provider: str
    attempted_providers: tuple[str, ...]
    failure_classifications: tuple[tuple[str, FailureClassification], ...]
    selected_provider: str | None
    cached_used: bool
    cached_age_seconds: int | None
    cached_snapshot_id: str | None
    reason_code: QuoteSelectionReason
    policy_id: str = BOOKMAKER_FALLBACK_POLICY_ID
    data_currency: str = "current"

    def __post_init__(self) -> None:
        if self.cached_used and self.data_currency == "current":
            msg = "cached stale data must never be labelled current"
            raise PermanentSourceError(msg)
        if self.cached_used and self.cached_snapshot_id is None:
            msg = "cached_used requires cached_snapshot_id"
            raise PermanentSourceError(msg)


def resolve_provider_fallback(
    *,
    preferred_attempt: ProviderAttemptOutcome,
    comparison_attempt: ProviderAttemptOutcome | None,
    additional_attempts: tuple[ProviderAttemptOutcome, ...] = (),
    enabled_additional_providers: tuple[str, ...] = DEFAULT_ADDITIONAL_FALLBACK_PROVIDERS,
    cached_snapshot: CachedSnapshotReference | None = None,
) -> ProviderFallbackDecision:
    """Resolve Betano preferred / Betclic first-fallback acquisition outcome.

    No third-party fallback provider is enabled by default. When both bookmakers
    fail, the last valid snapshot reference may be preserved and reported as
    stale/unavailable, never as current.
    """
    if preferred_attempt.provider_id != PROVIDER_BETANO_PT:
        msg = "preferred attempt must be betano-pt"
        raise PermanentSourceError(msg)
    if comparison_attempt is not None and comparison_attempt.provider_id != PROVIDER_BETCLIC_PT:
        msg = "comparison attempt must be betclic-pt when provided"
        raise PermanentSourceError(msg)
    for attempt in additional_attempts:
        if attempt.provider_id not in enabled_additional_providers:
            msg = f"additional provider {attempt.provider_id} is not enabled"
            raise PermanentSourceError(msg)

    attempted: list[str] = [preferred_attempt.provider_id]
    classifications: list[tuple[str, FailureClassification]] = [
        (preferred_attempt.provider_id, preferred_attempt.failure_classification)
    ]

    if preferred_attempt.success:
        return ProviderFallbackDecision(
            preferred_provider=PROVIDER_BETANO_PT,
            attempted_providers=tuple(attempted),
            failure_classifications=tuple(classifications),
            selected_provider=PROVIDER_BETANO_PT,
            cached_used=False,
            cached_age_seconds=None,
            cached_snapshot_id=None,
            reason_code=QuoteSelectionReason.PREFERRED_ONLY,
            data_currency="current",
        )

    if comparison_attempt is not None:
        attempted.append(comparison_attempt.provider_id)
        classifications.append(
            (comparison_attempt.provider_id, comparison_attempt.failure_classification)
        )
        if comparison_attempt.success:
            return ProviderFallbackDecision(
                preferred_provider=PROVIDER_BETANO_PT,
                attempted_providers=tuple(attempted),
                failure_classifications=tuple(classifications),
                selected_provider=PROVIDER_BETCLIC_PT,
                cached_used=False,
                cached_age_seconds=None,
                cached_snapshot_id=None,
                reason_code=QuoteSelectionReason.COMPARISON_FALLBACK,
                data_currency="current",
            )

    for attempt in additional_attempts:
        attempted.append(attempt.provider_id)
        classifications.append((attempt.provider_id, attempt.failure_classification))
        if attempt.success:
            return ProviderFallbackDecision(
                preferred_provider=PROVIDER_BETANO_PT,
                attempted_providers=tuple(attempted),
                failure_classifications=tuple(classifications),
                selected_provider=attempt.provider_id,
                cached_used=False,
                cached_age_seconds=None,
                cached_snapshot_id=None,
                reason_code=QuoteSelectionReason.COMPARISON_FALLBACK,
                data_currency="current",
            )

    if cached_snapshot is not None:
        return ProviderFallbackDecision(
            preferred_provider=PROVIDER_BETANO_PT,
            attempted_providers=tuple(attempted),
            failure_classifications=tuple(classifications),
            selected_provider=None,
            cached_used=True,
            cached_age_seconds=cached_snapshot.age_seconds,
            cached_snapshot_id=cached_snapshot.snapshot_id,
            reason_code=QuoteSelectionReason.CACHED_STALE_PRESERVED,
            data_currency="stale",
        )

    return ProviderFallbackDecision(
        preferred_provider=PROVIDER_BETANO_PT,
        attempted_providers=tuple(attempted),
        failure_classifications=tuple(classifications),
        selected_provider=None,
        cached_used=False,
        cached_age_seconds=None,
        cached_snapshot_id=None,
        reason_code=QuoteSelectionReason.PROVIDER_UNAVAILABLE,
        data_currency="unavailable",
    )

"""Fallback decision and provider-status evidence tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sports_analytics.bookmakers.fallback import (
    CachedSnapshotReference,
    ProviderAttemptOutcome,
    resolve_provider_fallback,
)
from sports_analytics.bookmakers.status import build_provider_status
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    FailureClassification,
    ProviderStatusCode,
    QuoteSelectionReason,
)
from sports_analytics.core.exceptions import PermanentSourceError

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_preferred_success_short_circuits_fallback() -> None:
    decision = resolve_provider_fallback(
        preferred_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETANO_PT,
            success=True,
            failure_classification=FailureClassification.NONE,
        ),
        comparison_attempt=None,
    )
    assert decision.selected_provider == PROVIDER_BETANO_PT
    assert decision.cached_used is False
    assert decision.data_currency == "current"
    assert decision.reason_code is QuoteSelectionReason.PREFERRED_ONLY


def test_comparison_fallback_when_preferred_fails() -> None:
    decision = resolve_provider_fallback(
        preferred_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETANO_PT,
            success=False,
            failure_classification=FailureClassification.BLOCKED,
            block_or_failure_code="captcha",
        ),
        comparison_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETCLIC_PT,
            success=True,
            failure_classification=FailureClassification.NONE,
        ),
    )
    assert decision.selected_provider == PROVIDER_BETCLIC_PT
    assert decision.reason_code is QuoteSelectionReason.COMPARISON_FALLBACK
    assert decision.attempted_providers == (PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT)


def test_stale_cache_preserved_and_never_labelled_current() -> None:
    with pytest.raises(PermanentSourceError, match="never be labelled current"):
        CachedSnapshotReference(
            snapshot_id="snap-1",
            observed_at_utc=NOW,
            age_seconds=120,
            is_current=True,
        )
    decision = resolve_provider_fallback(
        preferred_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETANO_PT,
            success=False,
            failure_classification=FailureClassification.RETRYABLE,
        ),
        comparison_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETCLIC_PT,
            success=False,
            failure_classification=FailureClassification.PERMANENT,
        ),
        cached_snapshot=CachedSnapshotReference(
            snapshot_id="snap-stale",
            observed_at_utc=NOW,
            age_seconds=900,
        ),
    )
    assert decision.cached_used is True
    assert decision.data_currency == "stale"
    assert decision.selected_provider is None
    assert decision.reason_code is QuoteSelectionReason.CACHED_STALE_PRESERVED


def test_unavailable_when_no_provider_and_no_cache() -> None:
    decision = resolve_provider_fallback(
        preferred_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETANO_PT,
            success=False,
            failure_classification=FailureClassification.PERMANENT,
        ),
        comparison_attempt=ProviderAttemptOutcome(
            provider_id=PROVIDER_BETCLIC_PT,
            success=False,
            failure_classification=FailureClassification.PERMANENT,
        ),
    )
    assert decision.selected_provider is None
    assert decision.cached_used is False
    assert decision.data_currency == "unavailable"
    assert decision.reason_code is QuoteSelectionReason.PROVIDER_UNAVAILABLE


def test_status_never_operational_without_success_evidence() -> None:
    from sports_analytics.bookmakers.status import ProviderStatusRecord

    with pytest.raises(PermanentSourceError, match="missing evidence"):
        ProviderStatusRecord(
            provider_id=PROVIDER_BETANO_PT,
            status_code=ProviderStatusCode.OPERATIONAL,
            last_attempted_acquisition_utc=NOW,
            last_successful_acquisition_utc=None,
            last_valid_snapshot_id=None,
            snapshot_age_seconds=None,
            events_observed=1,
            valid_quotes_observed=1,
            unresolved_events=0,
            rejected_markets=0,
            warnings=(),
            current_block_or_failure_classification=FailureClassification.NONE,
            next_eligible_attempt_utc=None,
            adapter_version="betano-pt-adapter-v1",
            observed_at_utc=NOW,
        )


def test_build_provider_status_classifies_unknown_and_operational() -> None:
    unknown = build_provider_status(
        provider_id=PROVIDER_BETANO_PT,
        adapter_version="betano-pt-adapter-v1",
        observed_at_utc=NOW,
        last_attempted_acquisition_utc=None,
        last_successful_acquisition_utc=None,
        last_valid_snapshot_id=None,
        snapshot_age_seconds=None,
        events_observed=0,
        valid_quotes_observed=0,
        unresolved_events=0,
        rejected_markets=0,
        warnings=(),
        current_block_or_failure_classification=FailureClassification.NONE,
        next_eligible_attempt_utc=None,
    )
    assert unknown.status_code is ProviderStatusCode.UNKNOWN

    operational = build_provider_status(
        provider_id=PROVIDER_BETANO_PT,
        adapter_version="betano-pt-adapter-v1",
        observed_at_utc=NOW,
        last_attempted_acquisition_utc=NOW,
        last_successful_acquisition_utc=NOW,
        last_valid_snapshot_id="snap-1",
        snapshot_age_seconds=0,
        events_observed=2,
        valid_quotes_observed=4,
        unresolved_events=0,
        rejected_markets=0,
        warnings=(),
        current_block_or_failure_classification=FailureClassification.NONE,
        next_eligible_attempt_utc=None,
    )
    assert operational.status_code is ProviderStatusCode.OPERATIONAL

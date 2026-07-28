"""Typed provider status records for bookmaker acquisition surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sports_analytics.bookmakers.types import (
    FailureClassification,
    ProviderStatusCode,
)
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.data.types import validate_identifier
from sports_analytics.sports.contracts import require_utc


@dataclass(frozen=True, slots=True)
class ProviderStatusRecord:
    """Operational status of one bookmaker provider for later UI use.

    Missing evidence is never classified as operational.
    """

    provider_id: str
    status_code: ProviderStatusCode
    last_attempted_acquisition_utc: datetime | None
    last_successful_acquisition_utc: datetime | None
    last_valid_snapshot_id: str | None
    snapshot_age_seconds: int | None
    events_observed: int
    valid_quotes_observed: int
    unresolved_events: int
    rejected_markets: int
    warnings: tuple[str, ...]
    current_block_or_failure_classification: FailureClassification
    next_eligible_attempt_utc: datetime | None
    adapter_version: str
    observed_at_utc: datetime
    provider_native_markets: int = 0
    provider_native_priced_selections: int = 0
    canonical_markets: int = 0
    canonical_quotes: int = 0
    unmapped_markets: int = 0
    non_comparable_quotes: int = 0
    complete_events: int = 0
    partial_events: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.provider_id, field_name="provider_id")
        validate_identifier(self.adapter_version, field_name="adapter_version")
        object.__setattr__(
            self,
            "observed_at_utc",
            require_utc(self.observed_at_utc, field_name="observed_at_utc"),
        )
        if self.last_attempted_acquisition_utc is not None:
            object.__setattr__(
                self,
                "last_attempted_acquisition_utc",
                require_utc(
                    self.last_attempted_acquisition_utc,
                    field_name="last_attempted_acquisition_utc",
                ),
            )
        if self.last_successful_acquisition_utc is not None:
            object.__setattr__(
                self,
                "last_successful_acquisition_utc",
                require_utc(
                    self.last_successful_acquisition_utc,
                    field_name="last_successful_acquisition_utc",
                ),
            )
        if self.next_eligible_attempt_utc is not None:
            object.__setattr__(
                self,
                "next_eligible_attempt_utc",
                require_utc(
                    self.next_eligible_attempt_utc,
                    field_name="next_eligible_attempt_utc",
                ),
            )
        if self.snapshot_age_seconds is not None and self.snapshot_age_seconds < 0:
            msg = "snapshot_age_seconds must be non-negative"
            raise PermanentSourceError(msg)
        for field_name in (
            "events_observed",
            "valid_quotes_observed",
            "unresolved_events",
            "rejected_markets",
            "provider_native_markets",
            "provider_native_priced_selections",
            "canonical_markets",
            "canonical_quotes",
            "unmapped_markets",
            "non_comparable_quotes",
            "complete_events",
            "partial_events",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                msg = f"{field_name} must be a non-negative int"
                raise PermanentSourceError(msg)
        if tuple(sorted(self.warnings)) != self.warnings:
            msg = "warnings must be sorted"
            raise PermanentSourceError(msg)
        if (
            self.status_code is ProviderStatusCode.OPERATIONAL
            and self.last_successful_acquisition_utc is None
        ):
            msg = "missing evidence must never be classified as operational"
            raise PermanentSourceError(msg)


def build_provider_status(
    *,
    provider_id: str,
    adapter_version: str,
    observed_at_utc: datetime,
    last_attempted_acquisition_utc: datetime | None,
    last_successful_acquisition_utc: datetime | None,
    last_valid_snapshot_id: str | None,
    snapshot_age_seconds: int | None,
    events_observed: int,
    valid_quotes_observed: int,
    unresolved_events: int,
    rejected_markets: int,
    warnings: tuple[str, ...],
    current_block_or_failure_classification: FailureClassification,
    next_eligible_attempt_utc: datetime | None,
    blocked: bool = False,
    drift_detected: bool = False,
    disabled: bool = False,
    acquisition_partial: bool = False,
    provider_native_markets: int = 0,
    provider_native_priced_selections: int = 0,
    canonical_markets: int = 0,
    canonical_quotes: int = 0,
    unmapped_markets: int = 0,
    non_comparable_quotes: int = 0,
    complete_events: int = 0,
    partial_events: int = 0,
) -> ProviderStatusRecord:
    """Build a provider status record with conservative classification rules."""
    status = _classify_status(
        last_successful_acquisition_utc=last_successful_acquisition_utc,
        last_attempted_acquisition_utc=last_attempted_acquisition_utc,
        snapshot_age_seconds=snapshot_age_seconds,
        events_observed=events_observed,
        valid_quotes_observed=valid_quotes_observed,
        unresolved_events=unresolved_events,
        rejected_markets=rejected_markets,
        blocked=blocked,
        drift_detected=drift_detected,
        disabled=disabled,
        acquisition_partial=acquisition_partial,
        failure_classification=current_block_or_failure_classification,
    )
    return ProviderStatusRecord(
        provider_id=provider_id,
        status_code=status,
        last_attempted_acquisition_utc=last_attempted_acquisition_utc,
        last_successful_acquisition_utc=last_successful_acquisition_utc,
        last_valid_snapshot_id=last_valid_snapshot_id,
        snapshot_age_seconds=snapshot_age_seconds,
        events_observed=events_observed,
        valid_quotes_observed=valid_quotes_observed,
        unresolved_events=unresolved_events,
        rejected_markets=rejected_markets,
        warnings=tuple(sorted(warnings)),
        current_block_or_failure_classification=current_block_or_failure_classification,
        next_eligible_attempt_utc=next_eligible_attempt_utc,
        adapter_version=adapter_version,
        observed_at_utc=observed_at_utc,
        provider_native_markets=provider_native_markets,
        provider_native_priced_selections=provider_native_priced_selections,
        canonical_markets=canonical_markets,
        canonical_quotes=canonical_quotes,
        unmapped_markets=unmapped_markets,
        non_comparable_quotes=non_comparable_quotes,
        complete_events=complete_events,
        partial_events=partial_events,
    )


def _classify_status(
    *,
    last_successful_acquisition_utc: datetime | None,
    last_attempted_acquisition_utc: datetime | None,
    snapshot_age_seconds: int | None,
    events_observed: int,
    valid_quotes_observed: int,
    unresolved_events: int,
    rejected_markets: int,
    blocked: bool,
    drift_detected: bool,
    disabled: bool,
    acquisition_partial: bool,
    failure_classification: FailureClassification,
) -> ProviderStatusCode:
    if disabled:
        return ProviderStatusCode.DISABLED
    if blocked or failure_classification is FailureClassification.BLOCKED:
        return ProviderStatusCode.BLOCKED
    if drift_detected:
        return ProviderStatusCode.DRIFT_DETECTED
    if last_successful_acquisition_utc is None:
        if last_attempted_acquisition_utc is None and events_observed == 0:
            return ProviderStatusCode.UNKNOWN
        return ProviderStatusCode.UNAVAILABLE
    if snapshot_age_seconds is not None and snapshot_age_seconds > 0 and events_observed == 0:
        return ProviderStatusCode.STALE
    if acquisition_partial or unresolved_events > 0 or rejected_markets > 0:
        if valid_quotes_observed > 0:
            return ProviderStatusCode.PARTIAL
        return ProviderStatusCode.PARTIAL
    if valid_quotes_observed <= 0 and events_observed <= 0:
        return ProviderStatusCode.UNKNOWN
    return ProviderStatusCode.OPERATIONAL

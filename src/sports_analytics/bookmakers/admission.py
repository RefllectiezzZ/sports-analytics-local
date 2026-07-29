"""Admission policy for bookmaker snapshot publication."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sports_analytics.bookmakers.normalization import NormalizedBookmakerBundle
from sports_analytics.sources.bookmaker_contracts import (
    CompletenessState,
    ProviderAcquisitionBundle,
    provider_native_markets,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult


class AdmissionOutcome(StrEnum):
    """Typed admission decision before snapshot publication."""

    ADMITTED = "admitted"
    PARTIAL = "partial"
    DRIFT_DETECTED = "drift-detected"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    FAILED = "failed"


#: Drift codes that remain auditable without blocking an otherwise valid cycle.
_INFORMATIONAL_DRIFT_CODES: frozenset[str] = frozenset(
    {
        "unknown-market",
        "live-event-excluded",
        "non-football-excluded",
        "non-prematch-excluded",
        "malformed-event-reference",
        "malformed-market-reference",
        "malformed-selection-reference",
        "malformed-market-list",
        "malformed-selection-list",
        "malformed-event",
        "malformed-market",
        "malformed-selection",
        "event-without-supported-markets",
        "participant-count-rejected",
        "participant-identity-rejected",
        "competition-identity-rejected",
        "selection-mapping-rejected",
        "market-typeid-mismatch",
        "missing-total-line",
        "contradictory-line",
        "duplicate-selection-id",
        "event-outside-window",
        "non-prematch-event-excluded",
        "event-before-window",
        "event-at-or-after-window",
        "event-limit-truncated",
    }
)


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Explicit typed admission decision for one acquisition cycle."""

    outcome: AdmissionOutcome
    reason_code: str
    may_publish: bool
    may_replace_last_valid: bool
    warnings: tuple[str, ...]
    provider_inventory_admitted: bool = False
    canonical_projection_admitted: bool = False
    comparison_catalogue_admitted: bool = False
    exhaustive_capture_complete: bool = False


def evaluate_admission(
    *,
    browser_result: BrowserAcquisitionResult,
    bundle: ProviderAcquisitionBundle,
    normalized: NormalizedBookmakerBundle,
    valid_quote_count: int,
    unresolved_event_count: int,
    verified_extraction_applied: bool,
) -> AdmissionDecision:
    """Decide whether a current snapshot may replace the last valid snapshot."""
    warnings = tuple(sorted({warning.code for warning in bundle.warnings}))
    if browser_result.block_reason is not None:
        return AdmissionDecision(
            outcome=AdmissionOutcome.BLOCKED,
            reason_code=browser_result.block_reason.value,
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    if not verified_extraction_applied:
        return AdmissionDecision(
            outcome=AdmissionOutcome.UNAVAILABLE,
            reason_code="no-verified-extraction-profile",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    if bundle.drift_codes and (
        "unknown-schema" in bundle.drift_codes or "schema-contradiction" in bundle.drift_codes
    ):
        return AdmissionDecision(
            outcome=AdmissionOutcome.DRIFT_DETECTED,
            reason_code="critical-parser-drift",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    if len(bundle.events) == 0:
        return AdmissionDecision(
            outcome=AdmissionOutcome.UNAVAILABLE,
            reason_code="no-supported-events",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    blocking_drift = tuple(
        sorted(code for code in bundle.drift_codes if code not in _INFORMATIONAL_DRIFT_CODES)
    )
    if blocking_drift:
        return AdmissionDecision(
            outcome=AdmissionOutcome.DRIFT_DETECTED,
            reason_code="parser-drift",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    native_market_count = sum(len(provider_native_markets(event)) for event in bundle.events)
    native_priced_selection_count = sum(
        sum(selection.decimal_odds is not None for selection in market.selections)
        for event in bundle.events
        for market in provider_native_markets(event)
    )
    legacy_projection_only = native_market_count == 0 and valid_quote_count > 0
    provider_inventory_admitted = (
        native_market_count > 0 and native_priced_selection_count > 0
    ) or legacy_projection_only
    if not provider_inventory_admitted:
        return AdmissionDecision(
            outcome=AdmissionOutcome.UNAVAILABLE,
            reason_code="no-valid-provider-native-inventory",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    canonical_projection_admitted = valid_quote_count > 0
    comparison_catalogue_admitted = any(
        item.comparable and item.eligible for item in normalized.comparison_eligibility
    )
    complete_states = {
        CompletenessState.COMPLETE_BY_PROVIDER_REFERENCE,
        CompletenessState.COMPLETE_BY_REVIEWED_EVENT_PAYLOAD,
    }
    exhaustive_capture_complete = bool(bundle.events) and all(
        event.completeness.completeness_state in complete_states for event in bundle.events
    )
    if legacy_projection_only:
        exhaustive_capture_complete = True
    if unresolved_event_count > 0 or not exhaustive_capture_complete or normalized.unknown_markets:
        return AdmissionDecision(
            outcome=AdmissionOutcome.PARTIAL,
            reason_code="partial-acquisition",
            may_publish=not legacy_projection_only,
            may_replace_last_valid=False,
            warnings=warnings,
            provider_inventory_admitted=True,
            canonical_projection_admitted=canonical_projection_admitted,
            comparison_catalogue_admitted=comparison_catalogue_admitted,
            exhaustive_capture_complete=False,
        )
    return AdmissionDecision(
        outcome=AdmissionOutcome.ADMITTED,
        reason_code="admitted",
        may_publish=True,
        may_replace_last_valid=True,
        warnings=warnings,
        provider_inventory_admitted=True,
        canonical_projection_admitted=canonical_projection_admitted,
        comparison_catalogue_admitted=comparison_catalogue_admitted,
        exhaustive_capture_complete=True,
    )

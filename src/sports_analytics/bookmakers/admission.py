"""Admission policy for bookmaker snapshot publication."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sports_analytics.bookmakers.normalization import NormalizedBookmakerBundle
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult


class AdmissionOutcome(StrEnum):
    """Typed admission decision before snapshot publication."""

    ADMITTED = "admitted"
    PARTIAL = "partial"
    DRIFT_DETECTED = "drift-detected"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Explicit typed admission decision for one acquisition cycle."""

    outcome: AdmissionOutcome
    reason_code: str
    may_publish: bool
    may_replace_last_valid: bool
    warnings: tuple[str, ...]


def evaluate_admission(
    *,
    browser_result: BrowserAcquisitionResult,
    bundle: ProviderAcquisitionBundle,
    normalized: NormalizedBookmakerBundle,
    valid_quote_count: int,
    unresolved_event_count: int,
    native_payload_recognized: bool,
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
    if not native_payload_recognized:
        return AdmissionDecision(
            outcome=AdmissionOutcome.UNAVAILABLE,
            reason_code="no-native-payload",
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
    if valid_quote_count <= 0:
        return AdmissionDecision(
            outcome=AdmissionOutcome.UNAVAILABLE,
            reason_code="no-valid-quotes",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    if unresolved_event_count > 0 and len(bundle.events) == unresolved_event_count:
        return AdmissionDecision(
            outcome=AdmissionOutcome.PARTIAL,
            reason_code="all-events-unresolved",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    if bundle.drift_codes:
        return AdmissionDecision(
            outcome=AdmissionOutcome.DRIFT_DETECTED,
            reason_code="parser-drift",
            may_publish=False,
            may_replace_last_valid=False,
            warnings=warnings,
        )
    if unresolved_event_count > 0 or normalized.unknown_markets:
        return AdmissionDecision(
            outcome=AdmissionOutcome.PARTIAL,
            reason_code="partial-acquisition",
            may_publish=True,
            may_replace_last_valid=True,
            warnings=warnings,
        )
    return AdmissionDecision(
        outcome=AdmissionOutcome.ADMITTED,
        reason_code="admitted",
        may_publish=True,
        may_replace_last_valid=True,
        warnings=warnings,
    )

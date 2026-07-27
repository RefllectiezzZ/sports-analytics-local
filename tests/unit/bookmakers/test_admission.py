"""Admission policy tests for bookmaker snapshot publication."""

from __future__ import annotations

from datetime import UTC, datetime

from sports_analytics.bookmakers.admission import AdmissionOutcome, evaluate_admission
from sports_analytics.bookmakers.normalization import NormalizedBookmakerBundle
from sports_analytics.bookmakers.types import BOOKMAKER_SCHEMA_VERSION
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.browser.contracts import (
    BrowserAcquisitionResult,
    BrowserBlockReason,
    BrowserMode,
)

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _browser(*, blocked: bool = False) -> BrowserAcquisitionResult:
    return BrowserAcquisitionResult(
        provider_id="betano-pt",
        sport="football",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=OBSERVED,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=BrowserBlockReason.ACCESS_DENIED if blocked else None,
        warnings=(),
    )


def _bundle(*, drift: tuple[str, ...] = ()) -> ProviderAcquisitionBundle:
    return ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="betano-pt-adapter-v1",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=OBSERVED,
        sport="football",
        events=(),
        warnings=(),
        drift_codes=drift,
        provenance=(),
    )


def _normalized() -> NormalizedBookmakerBundle:
    return NormalizedBookmakerBundle(
        acquisition_metadata=(),
        participants=(),
        source_events=(),
        events=(),
        market_quotes=(),
        parser_drift_findings=(),
        comparison_eligibility=(),
        unknown_markets=(),
        warnings=(),
        normalizer_version="bookmaker-normalizer-v1",
        reconciliation_policy_version="bookmaker-event-reconciliation-v1",
        participant_reconciliation_policy_version="bookmaker-event-reconciliation-v1",
        schema_version=BOOKMAKER_SCHEMA_VERSION,
    )


def test_blocked_browser_never_admits() -> None:
    decision = evaluate_admission(
        browser_result=_browser(blocked=True),
        bundle=_bundle(),
        normalized=_normalized(),
        valid_quote_count=0,
        unresolved_event_count=0,
        verified_extraction_applied=False,
    )
    assert decision.outcome is AdmissionOutcome.BLOCKED
    assert decision.may_publish is False
    assert decision.may_replace_last_valid is False


def test_empty_unverified_extraction_rejected() -> None:
    decision = evaluate_admission(
        browser_result=_browser(),
        bundle=_bundle(drift=("no-verified-extraction-profile",)),
        normalized=_normalized(),
        valid_quote_count=0,
        unresolved_event_count=0,
        verified_extraction_applied=False,
    )
    assert decision.outcome is AdmissionOutcome.UNAVAILABLE
    assert decision.may_replace_last_valid is False


def test_partial_never_replaces_valid_snapshot() -> None:
    from datetime import timedelta

    from sports_analytics.sources.bookmaker_contracts import (
        ProviderEventObservation,
        ProviderEventState,
        ProviderParticipantObservation,
    )

    event = ProviderEventObservation(
        source_event_id="e1",
        source_competition_id="c1",
        sport="football",
        scheduled_start_utc=OBSERVED + timedelta(hours=2),
        event_state=ProviderEventState.PRE_MATCH,
        participants=(
            ProviderParticipantObservation(
                source_participant_id="p1",
                display_name="Home",
                role="home",
            ),
            ProviderParticipantObservation(
                source_participant_id="p2",
                display_name="Away",
                role="away",
            ),
        ),
        markets=(),
        source_page_route_id="football-prematch",
    )
    bundle = ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="betano-pt-adapter-v1",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=OBSERVED,
        sport="football",
        events=(event,),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )
    decision = evaluate_admission(
        browser_result=_browser(),
        bundle=bundle,
        normalized=_normalized(),
        valid_quote_count=1,
        unresolved_event_count=1,
        verified_extraction_applied=True,
    )
    assert decision.outcome is AdmissionOutcome.PARTIAL
    assert decision.may_publish is False
    assert decision.may_replace_last_valid is False

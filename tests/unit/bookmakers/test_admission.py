"""Admission policy tests for bookmaker snapshot publication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_analytics.bookmakers.admission import AdmissionOutcome, evaluate_admission
from sports_analytics.bookmakers.normalization import NormalizedBookmakerBundle
from sports_analytics.bookmakers.types import BOOKMAKER_SCHEMA_VERSION
from sports_analytics.bookmakers.window import AcquisitionWindow, apply_acquisition_window
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.bookmaker_contracts import (
    CompletenessState,
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderMarketObservation,
    ProviderParticipantObservation,
    ProviderSelectionObservation,
)
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


def test_zero_valid_events_rejected_even_with_verified_extraction() -> None:
    decision = evaluate_admission(
        browser_result=_browser(),
        bundle=_bundle(drift=("competition-identity-rejected",)),
        normalized=_normalized(),
        valid_quote_count=0,
        unresolved_event_count=0,
        verified_extraction_applied=True,
    )
    assert decision.outcome is AdmissionOutcome.UNAVAILABLE
    assert decision.reason_code == "no-supported-events"
    assert decision.may_publish is False


def test_informational_drift_codes_do_not_block_admission() -> None:
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
        drift_codes=(
            "competition-identity-rejected",
            "live-event-excluded",
            "malformed-market-reference",
            "non-football-excluded",
            "unknown-market",
        ),
        provenance=(),
    )
    decision = evaluate_admission(
        browser_result=_browser(),
        bundle=bundle,
        normalized=_normalized(),
        valid_quote_count=2,
        unresolved_event_count=0,
        verified_extraction_applied=True,
    )
    assert decision.outcome is AdmissionOutcome.ADMITTED
    assert decision.may_publish is True


def test_blocking_drift_still_fails_closed() -> None:
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
        drift_codes=("schema-version-mismatch",),
        provenance=(),
    )
    decision = evaluate_admission(
        browser_result=_browser(),
        bundle=bundle,
        normalized=_normalized(),
        valid_quote_count=2,
        unresolved_event_count=0,
        verified_extraction_applied=True,
    )
    assert decision.outcome is AdmissionOutcome.DRIFT_DETECTED
    assert decision.may_publish is False


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


def test_event_limit_partial_inventory_may_publish_but_never_replace() -> None:
    market = ProviderMarketObservation(
        source_market_id="market-1",
        display_label="Native market",
        market_status=MarketStatus.OPEN,
        selections=(
            ProviderSelectionObservation(
                source_selection_id="selection-1",
                display_label="Selection",
                decimal_odds=Decimal("2.00"),
                selection_status=SelectionStatus.ACTIVE,
            ),
        ),
    )

    def _event(identity: str) -> ProviderEventObservation:
        return ProviderEventObservation(
            source_event_id=identity,
            source_competition_id="c1",
            sport="football",
            scheduled_start_utc=OBSERVED + timedelta(hours=2),
            event_state=ProviderEventState.PRE_MATCH,
            participants=(
                ProviderParticipantObservation("p1", "Home", "home"),
                ProviderParticipantObservation("p2", "Away", "away"),
            ),
            markets=(market,),
            native_markets=(market,),
            source_page_route_id="football-prematch",
        )

    bundle = ProviderAcquisitionBundle(
        provider_id="betano-pt",
        adapter_version="betano-pt-adapter-v1",
        acquisition_cycle_id="cycle-1",
        observed_at_utc=OBSERVED,
        sport="football",
        events=(_event("event-b"), _event("event-a")),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )
    limited = apply_acquisition_window(
        bundle,
        AcquisitionWindow(
            window_start_utc=OBSERVED,
            window_end_utc=OBSERVED + timedelta(hours=4),
            maximum_events=1,
        ),
    )
    decision = evaluate_admission(
        browser_result=_browser(),
        bundle=limited,
        normalized=_normalized(),
        valid_quote_count=1,
        unresolved_event_count=0,
        verified_extraction_applied=True,
    )
    assert limited.drift_codes == ("event-limit-truncated",)
    assert (
        limited.events[0].completeness.completeness_state is CompletenessState.PARTIAL_EVENT_LIMIT
    )
    assert decision.outcome is AdmissionOutcome.PARTIAL
    assert decision.may_publish is True
    assert decision.may_replace_last_valid is False
    assert decision.exhaustive_capture_complete is False

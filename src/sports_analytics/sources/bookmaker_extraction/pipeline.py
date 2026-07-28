"""Apply extraction profiles and parse adapter-contract payloads."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sports_analytics.sources.bookmaker_contracts import (
    CompletenessState,
    EventCompletenessEvidence,
    ParserDriftSeverity,
    ProviderAcquisitionBundle,
    ProviderParserWarning,
)
from sports_analytics.sources.bookmaker_extraction.adapter_contract import (
    parse_adapter_contract_payloads,
)
from sports_analytics.sources.bookmaker_extraction.contracts import (
    ExtractionProfile,
    ExtractionResult,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
from sports_analytics.sources.raw_capture import BookmakerRawCapture


def apply_extraction_profile(
    *,
    profile: ExtractionProfile | None,
    browser_result: BrowserAcquisitionResult,
    captures: tuple[BookmakerRawCapture, ...],
    adapter_version: str,
    parser_version: str,
    observed_at_utc: datetime,
    sport: str,
) -> ProviderAcquisitionBundle:
    """Run extraction (when installed) and parse internal adapter-contract payloads."""
    if profile is None:
        return ProviderAcquisitionBundle(
            provider_id=browser_result.provider_id,
            adapter_version=adapter_version,
            acquisition_cycle_id=browser_result.acquisition_cycle_id,
            observed_at_utc=observed_at_utc,
            sport=sport,
            events=(),
            warnings=(
                ProviderParserWarning(
                    code="no-verified-extraction-profile",
                    message="no verified extraction profile installed for provider",
                    severity=ParserDriftSeverity.ERROR,
                ),
            ),
            drift_codes=("no-verified-extraction-profile",),
            provenance=(),
        )
    extraction = profile.extract(browser_result=browser_result, captures=captures)
    return _bundle_from_extraction(
        extraction=extraction,
        browser_result=browser_result,
        adapter_version=adapter_version,
        parser_version=parser_version,
        observed_at_utc=observed_at_utc,
        sport=sport,
    )


def _bundle_from_extraction(
    *,
    extraction: ExtractionResult,
    browser_result: BrowserAcquisitionResult,
    adapter_version: str,
    parser_version: str,
    observed_at_utc: datetime,
    sport: str,
) -> ProviderAcquisitionBundle:
    extra_warnings = tuple(
        ProviderParserWarning(
            code="extraction-warning",
            message=message,
            severity=ParserDriftSeverity.WARNING,
        )
        for message in extraction.warnings
    )
    bundle = parse_adapter_contract_payloads(
        [dict(item) for item in extraction.adapter_contract_payloads],
        provider_id=browser_result.provider_id,
        adapter_version=adapter_version,
        acquisition_cycle_id=browser_result.acquisition_cycle_id,
        observed_at_utc=observed_at_utc,
        sport=sport,
        extra_warnings=extra_warnings,
        provenance=(f"extraction:{extraction.profile_id}",),
        parser_version=parser_version,
    )
    merged_drift = tuple(sorted(set(bundle.drift_codes) | set(extraction.drift_codes)))
    truncated_count = browser_result.truncated_response_count
    if truncated_count:
        merged_drift = tuple(sorted(set(merged_drift) | {"response-capture-truncated"}))
        bundle = replace(
            bundle,
            events=tuple(
                replace(
                    event,
                    completeness=EventCompletenessEvidence(
                        provider_declared_market_references=(
                            event.completeness.provider_declared_market_references
                        ),
                        market_groups_observed=(event.completeness.market_groups_observed),
                        markets_observed=event.completeness.markets_observed,
                        markets_parsed=event.completeness.markets_parsed,
                        markets_rejected=event.completeness.markets_rejected,
                        selections_observed=(event.completeness.selections_observed),
                        selections_parsed=event.completeness.selections_parsed,
                        selections_rejected=(event.completeness.selections_rejected),
                        markets_with_valid_price=(event.completeness.markets_with_valid_price),
                        source_responses_contributing=(
                            event.completeness.source_responses_contributing
                        ),
                        event_detail_surface_visited=(
                            event.completeness.event_detail_surface_visited
                        ),
                        event_detail_readiness_reached=(
                            event.completeness.event_detail_readiness_reached
                        ),
                        truncated_response_count=(
                            event.completeness.truncated_response_count + truncated_count
                        ),
                        bounded_response_rejection_count=(
                            event.completeness.bounded_response_rejection_count + truncated_count
                        ),
                        completeness_state=(CompletenessState.PARTIAL_TRUNCATED_RESPONSE),
                    ),
                )
                for event in bundle.events
            ),
        )
    if not extraction.verified:
        merged_drift = tuple(sorted(set(merged_drift) | {"unverified-extraction-profile"}))
    return ProviderAcquisitionBundle(
        provider_id=bundle.provider_id,
        adapter_version=bundle.adapter_version,
        acquisition_cycle_id=bundle.acquisition_cycle_id,
        observed_at_utc=bundle.observed_at_utc,
        sport=bundle.sport,
        events=bundle.events,
        warnings=bundle.warnings,
        drift_codes=merged_drift,
        provenance=bundle.provenance,
        recognized_profile_response_count=extraction.recognized_response_count,
    )

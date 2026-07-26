"""Deterministic Betano observation parser for sanitized fixture payloads.

Parsers consume browser observations or synthetic fixture JSON. They do not
import Playwright and must report schema drift instead of silently accepting
changed response shapes.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_analytics.core.exceptions import NormalizationError, ParserError
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.betano.catalog import PARSER_VERSION, PROVIDER_ID
from sports_analytics.sources.bookmaker_contracts import (
    ParserDriftSeverity,
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderMarketObservation,
    ProviderParserWarning,
    ProviderParticipantObservation,
    ProviderSelectionObservation,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
from sports_analytics.sources.browser.safety import classify_block_signals
from sports_analytics.sports.contracts import require_utc


def parse_betano_acquisition(
    acquisition: BrowserAcquisitionResult,
    *,
    adapter_version: str,
) -> ProviderAcquisitionBundle:
    """Parse browser observations into provider-domain records."""
    if acquisition.provider_id != PROVIDER_ID:
        msg = f"betano parser received provider {acquisition.provider_id}"
        raise ParserError(msg)
    if acquisition.block_reason is not None:
        return ProviderAcquisitionBundle(
            provider_id=PROVIDER_ID,
            adapter_version=adapter_version,
            acquisition_cycle_id=acquisition.acquisition_cycle_id,
            observed_at_utc=acquisition.observed_at_utc,
            sport=acquisition.sport,
            events=(),
            warnings=(
                ProviderParserWarning(
                    code="provider-blocked",
                    message=f"provider blocked: {acquisition.block_reason.value}",
                    severity=ParserDriftSeverity.ERROR,
                ),
            ),
            drift_codes=(),
            provenance=tuple(sorted({f"page:{page.page_route_id}" for page in acquisition.pages})),
        )
    payloads: list[dict[str, Any]] = []
    warnings: list[ProviderParserWarning] = []
    for response in acquisition.responses:
        try:
            payloads.append(_load_payload(response.body_text))
        except ParserError as exc:
            warnings.append(
                ProviderParserWarning(
                    code="json-parse-failed",
                    message=str(exc),
                    severity=ParserDriftSeverity.ERROR,
                    source_path=response.response_url,
                )
            )
    for page in acquisition.pages:
        detected = classify_block_signals(
            title=page.title,
            body_text=page.sanitized_dom_fragment,
        )
        if detected is not None:
            warnings.append(
                ProviderParserWarning(
                    code="blocked-page",
                    message=detected.value,
                    severity=ParserDriftSeverity.ERROR,
                )
            )
    return parse_betano_payloads(
        payloads,
        provider_id=PROVIDER_ID,
        adapter_version=adapter_version,
        acquisition_cycle_id=acquisition.acquisition_cycle_id,
        observed_at_utc=acquisition.observed_at_utc,
        sport=acquisition.sport,
        extra_warnings=tuple(warnings),
        provenance=tuple(
            sorted(
                {f"response:{item.response_url}" for item in acquisition.responses}
                | {f"page:{item.page_route_id}" for item in acquisition.pages}
            )
        ),
    )


def parse_betano_payloads(
    payloads: list[dict[str, Any]],
    *,
    provider_id: str,
    adapter_version: str,
    acquisition_cycle_id: str,
    observed_at_utc: datetime,
    sport: str,
    extra_warnings: tuple[ProviderParserWarning, ...] = (),
    provenance: tuple[str, ...] = (),
) -> ProviderAcquisitionBundle:
    """Parse one or more sanitized Betano fixture payloads."""
    events: list[ProviderEventObservation] = []
    warnings = list(extra_warnings)
    drift_codes: set[str] = set()
    for payload in payloads:
        schema = payload.get("schema")
        if schema != "betano-fixture-bundle-v1":
            drift_codes.add("unknown-schema")
            warnings.append(
                ProviderParserWarning(
                    code="schema-drift",
                    message=f"unexpected schema {schema!r}",
                    severity=ParserDriftSeverity.ERROR,
                )
            )
            continue
        for raw_event in payload.get("events", []):
            if not isinstance(raw_event, dict):
                drift_codes.add("malformed-event")
                continue
            try:
                events.append(_parse_event(raw_event, default_sport=sport))
            except (NormalizationError, ParserError) as exc:
                warnings.append(
                    ProviderParserWarning(
                        code="event-rejected",
                        message=str(exc),
                        severity=ParserDriftSeverity.WARNING,
                    )
                )
        for unknown in payload.get("unknown_markets", []):
            drift_codes.add("unknown-market")
            warnings.append(
                ProviderParserWarning(
                    code="unknown-market-retained",
                    message=f"unknown market retained for audit: {unknown}",
                    severity=ParserDriftSeverity.WARNING,
                )
            )
    return ProviderAcquisitionBundle(
        provider_id=provider_id,
        adapter_version=adapter_version,
        acquisition_cycle_id=acquisition_cycle_id,
        observed_at_utc=observed_at_utc,
        sport=sport,
        events=tuple(events),
        warnings=tuple(warnings),
        drift_codes=tuple(sorted(drift_codes)),
        provenance=tuple(sorted(set(provenance) | {f"parser:{PARSER_VERSION}"})),
    )


def _load_payload(body_text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(body_text)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON payload: {exc}"
        raise ParserError(msg) from exc
    if not isinstance(loaded, dict):
        msg = "payload must be a JSON object"
        raise ParserError(msg)
    return loaded


def _parse_event(raw: dict[str, Any], *, default_sport: str) -> ProviderEventObservation:
    sport = str(raw.get("sport") or default_sport)
    state_raw = str(raw.get("event_state") or "pre-match")
    try:
        state = ProviderEventState(state_raw)
    except ValueError as exc:
        msg = f"unknown event_state {state_raw!r}"
        raise ParserError(msg) from exc
    participants = tuple(
        ProviderParticipantObservation(
            source_participant_id=str(item["source_participant_id"]),
            display_name=str(item["display_name"]),
            role=str(item["role"]),
            normalized_name=(
                str(item["normalized_name"]) if item.get("normalized_name") is not None else None
            ),
        )
        for item in raw.get("participants", [])
    )
    markets = tuple(_parse_market(item) for item in raw.get("markets", []) if isinstance(item, dict))
    start = raw.get("scheduled_start_utc")
    if not isinstance(start, str):
        msg = "malformed scheduled_start_utc"
        raise ParserError(msg)
    try:
        scheduled = require_utc(datetime.fromisoformat(start.replace("Z", "+00:00")))
    except (TypeError, ValueError, NormalizationError) as exc:
        msg = "malformed scheduled_start_utc"
        raise ParserError(msg) from exc
    return ProviderEventObservation(
        source_event_id=str(raw["source_event_id"]),
        source_competition_id=str(raw["source_competition_id"]),
        sport=sport,
        scheduled_start_utc=scheduled,
        event_state=state,
        participants=participants,
        markets=markets,
        source_page_route_id=str(raw.get("source_page_route_id") or f"{sport}-prematch"),
        competition_display_name=(
            str(raw["competition_display_name"])
            if raw.get("competition_display_name") is not None
            else None
        ),
    )


def _parse_market(raw: dict[str, Any]) -> ProviderMarketObservation:
    status_raw = str(raw.get("market_status") or "open")
    try:
        market_status = MarketStatus(status_raw)
    except ValueError as exc:
        msg = f"unknown market_status {status_raw!r}"
        raise ParserError(msg) from exc
    selections: list[ProviderSelectionObservation] = []
    for item in raw.get("selections", []):
        if not isinstance(item, dict):
            continue
        if item.get("decimal_odds") is None:
            continue
        try:
            odds = Decimal(str(item["decimal_odds"]).replace(",", "."))
        except (InvalidOperation, KeyError) as exc:
            msg = "invalid decimal odds"
            raise ParserError(msg) from exc
        selection_status = SelectionStatus(str(item.get("selection_status") or "active"))
        line = None
        if item.get("line") is not None:
            line = Decimal(str(item["line"]).replace(",", "."))
        selections.append(
            ProviderSelectionObservation(
                source_selection_id=str(item["source_selection_id"]),
                display_label=str(item["display_label"]),
                decimal_odds=odds,
                selection_status=selection_status,
                line=line,
            )
        )
    market_line = None
    if raw.get("line") is not None:
        market_line = Decimal(str(raw["line"]).replace(",", "."))
    return ProviderMarketObservation(
        source_market_id=str(raw["source_market_id"]),
        display_label=str(raw["display_label"]),
        market_status=market_status,
        selections=tuple(selections),
        period=str(raw["period"]) if raw.get("period") is not None else None,
        line=market_line,
        overtime_scope=str(raw["overtime_scope"]) if raw.get("overtime_scope") is not None else None,
        rules_scope=str(raw["rules_scope"]) if raw.get("rules_scope") is not None else None,
        canonical_market_definition_id=(
            str(raw["canonical_market_definition_id"])
            if raw.get("canonical_market_definition_id") is not None
            else None
        ),
    )

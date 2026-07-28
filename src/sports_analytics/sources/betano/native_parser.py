"""Production Betano native offering parser (``betano-offering-v1``)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_analytics.core.exceptions import NormalizationError, ParserError
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.betano.catalog import PARSER_VERSION, PROVIDER_ID
from sports_analytics.sources.betano.native_mappings import (
    BETANO_NATIVE_SCHEMA,
    BETANO_PARTICIPANT_ROLE_MAPPINGS,
    BETANO_PERIOD_MAPPINGS,
    map_betano_market_type,
    map_betano_sport_code,
)
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
from sports_analytics.sports.contracts import require_utc

_BETANO_EVENT_STATE: dict[str, ProviderEventState] = {
    "NOT_STARTED": ProviderEventState.PRE_MATCH,
    "PREMATCH": ProviderEventState.PRE_MATCH,
}
_BETANO_MARKET_STATUS: dict[str, MarketStatus] = {
    "OPEN": MarketStatus.OPEN,
    "SUSPENDED": MarketStatus.SUSPENDED,
    "CLOSED": MarketStatus.CLOSED,
}
_BETANO_SELECTION_STATUS: dict[str, SelectionStatus] = {
    "ACTIVE": SelectionStatus.ACTIVE,
    "SUSPENDED": SelectionStatus.SUSPENDED,
}


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
                    source_path=f"route:{response.page_route_id}",
                )
            )
    return parse_betano_native_payloads(
        payloads,
        provider_id=PROVIDER_ID,
        adapter_version=adapter_version,
        acquisition_cycle_id=acquisition.acquisition_cycle_id,
        observed_at_utc=acquisition.observed_at_utc,
        sport=acquisition.sport,
        extra_warnings=tuple(warnings),
        provenance=tuple(
            sorted(
                {f"response-route:{item.page_route_id}" for item in acquisition.responses}
                | {f"page:{item.page_route_id}" for item in acquisition.pages}
            )
        ),
    )


def parse_betano_native_payloads(
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
    """Parse one or more provider-native Betano offering payloads."""
    events: list[ProviderEventObservation] = []
    warnings = list(extra_warnings)
    drift_codes: set[str] = set()
    native_recognized = False
    for payload in payloads:
        schema = payload.get("schema")
        if schema == "betano-fixture-bundle-v1":
            drift_codes.add("synthetic-schema-rejected")
            warnings.append(
                ProviderParserWarning(
                    code="synthetic-schema-rejected",
                    message="production parser rejects synthetic fixture envelope",
                    severity=ParserDriftSeverity.ERROR,
                )
            )
            continue
        if schema != BETANO_NATIVE_SCHEMA:
            drift_codes.add("unknown-schema")
            warnings.append(
                ProviderParserWarning(
                    code="schema-drift",
                    message=f"unexpected schema {schema!r}",
                    severity=ParserDriftSeverity.ERROR,
                )
            )
            continue
        native_recognized = True
        for raw_event in payload.get("events", []):
            if not isinstance(raw_event, dict):
                drift_codes.add("malformed-event")
                continue
            try:
                events.append(_parse_native_event(raw_event, default_sport=sport))
            except (NormalizationError, ParserError) as exc:
                warnings.append(
                    ProviderParserWarning(
                        code="event-rejected",
                        message=str(exc),
                        severity=ParserDriftSeverity.WARNING,
                    )
                )
        for unsupported in payload.get("unsupportedMarkets", []):
            drift_codes.add("unknown-market")
            label = unsupported.get("name") if isinstance(unsupported, dict) else str(unsupported)
            warnings.append(
                ProviderParserWarning(
                    code="unknown-market-retained",
                    message=f"unknown market retained for audit: {label}",
                    severity=ParserDriftSeverity.WARNING,
                )
            )
    if not native_recognized and payloads and "synthetic-schema-rejected" not in drift_codes:
        drift_codes.add("no-native-payload")
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


def _parse_native_event(raw: dict[str, Any], *, default_sport: str) -> ProviderEventObservation:
    sport_code = str(raw.get("sportCode") or "")
    sport = map_betano_sport_code(sport_code, default_sport=default_sport)
    state_raw = str(raw.get("state") or "NOT_STARTED").upper()
    state = _BETANO_EVENT_STATE.get(state_raw)
    if state is None:
        msg = f"unsupported event state {state_raw!r}"
        raise ParserError(msg)
    if state is not ProviderEventState.PRE_MATCH:
        msg = "live events are not supported in PR #11"
        raise ParserError(msg)
    participants = tuple(
        ProviderParticipantObservation(
            source_participant_id=str(item["participantId"]),
            display_name=str(item["name"]),
            role=BETANO_PARTICIPANT_ROLE_MAPPINGS.get(
                str(item.get("role", "")).upper(),
                str(item.get("role", "unknown")).lower(),
            ),
            normalized_name=(
                str(item["normalizedName"]).lower()
                if item.get("normalizedName") is not None
                else None
            ),
        )
        for item in raw.get("participants", [])
        if isinstance(item, dict)
    )
    markets = tuple(
        _parse_native_market(item) for item in raw.get("markets", []) if isinstance(item, dict)
    )
    start = raw.get("startTimeUtc")
    if not isinstance(start, str):
        msg = "malformed startTimeUtc"
        raise ParserError(msg)
    try:
        scheduled = require_utc(
            datetime.fromisoformat(start.replace("Z", "+00:00")),
            field_name="startTimeUtc",
        )
    except (TypeError, ValueError, NormalizationError) as exc:
        msg = "malformed startTimeUtc"
        raise ParserError(msg) from exc
    return ProviderEventObservation(
        source_event_id=str(raw["eventId"]),
        source_competition_id=str(raw["competitionId"]),
        sport=sport,
        scheduled_start_utc=scheduled,
        event_state=state,
        participants=participants,
        markets=markets,
        source_page_route_id=str(raw.get("sourcePageRouteId") or f"{sport}-prematch"),
        competition_display_name=(
            str(raw["competitionName"]) if raw.get("competitionName") is not None else None
        ),
    )


def _parse_native_market(raw: dict[str, Any]) -> ProviderMarketObservation:
    type_code = str(raw.get("marketTypeCode") or "")
    canonical = map_betano_market_type(type_code)
    status_raw = str(raw.get("status") or "OPEN").upper()
    market_status = _BETANO_MARKET_STATUS.get(status_raw, MarketStatus.OPEN)
    period_code = raw.get("period")
    period = (
        BETANO_PERIOD_MAPPINGS.get(str(period_code).upper()) if period_code is not None else None
    )
    selections: list[ProviderSelectionObservation] = []
    seen: set[str] = set()
    for item in raw.get("selections", []):
        if not isinstance(item, dict):
            continue
        sel_id = str(item.get("selectionId", ""))
        if not sel_id or sel_id in seen:
            msg = "duplicate source selection identities"
            raise ParserError(msg)
        seen.add(sel_id)
        if item.get("price") is None:
            continue
        try:
            odds = Decimal(str(item["price"]).replace(",", "."))
        except (InvalidOperation, KeyError) as exc:
            msg = "invalid decimal odds"
            raise ParserError(msg) from exc
        sel_status = _BETANO_SELECTION_STATUS.get(
            str(item.get("status", "ACTIVE")).upper(),
            SelectionStatus.ACTIVE,
        )
        line = None
        if item.get("line") is not None:
            line = Decimal(str(item["line"]).replace(",", "."))
        selections.append(
            ProviderSelectionObservation(
                source_selection_id=sel_id,
                display_label=str(item.get("name", sel_id)),
                decimal_odds=odds,
                selection_status=sel_status,
                line=line,
            )
        )
    market_line = None
    if raw.get("line") is not None:
        market_line = Decimal(str(raw["line"]).replace(",", "."))
    return ProviderMarketObservation(
        source_market_id=str(raw["marketId"]),
        display_label=str(raw.get("name", type_code)),
        market_status=market_status,
        selections=tuple(selections),
        period=period,
        line=market_line,
        overtime_scope=(
            str(raw["overtimeScope"]) if raw.get("overtimeScope") is not None else None
        ),
        rules_scope=str(raw["rulesScope"]) if raw.get("rulesScope") is not None else None,
        canonical_market_definition_id=canonical,
    )

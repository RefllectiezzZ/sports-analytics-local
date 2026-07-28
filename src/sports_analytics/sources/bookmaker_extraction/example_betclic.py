"""Example/synthetic Betclic extraction profile for tests only (not provider-native)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sports_analytics.core.exceptions import ParserError
from sports_analytics.snapshots.paths import resolve_raw_path
from sports_analytics.sources.betclic.catalog import PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.adapter_contract import load_json_payload
from sports_analytics.sources.bookmaker_extraction.contracts import (
    ADAPTER_CONTRACT_SCHEMA,
    ExtractionResult,
)
from sports_analytics.sources.bookmaker_extraction.example_betano import (
    _sport_code,
    _translate_market,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
from sports_analytics.sources.raw_capture import BookmakerRawCapture

EXAMPLE_BETCLIC_PROFILE_ID = "example-betclic-synthetic-v1"
SYNTHETIC_FIXTURE_SCHEMA = "betclic-fixture-bundle-v1"


class ExampleBetclicSyntheticExtractionProfile:
    """Translate test-only ``betclic-fixture-bundle-v1`` captures to adapter contract."""

    profile_id = EXAMPLE_BETCLIC_PROFILE_ID
    verified = False
    provider_id = PROVIDER_ID
    raw_directory: Path | None = None

    def extract(
        self,
        *,
        browser_result: BrowserAcquisitionResult,
        captures: tuple[BookmakerRawCapture, ...],
    ) -> ExtractionResult:
        del browser_result
        payloads: list[dict[str, object]] = []
        warnings: list[str] = []
        drift_codes: list[str] = []
        for capture in captures:
            if capture.capture_kind != "provider-json":
                continue
            try:
                raw = load_json_payload(
                    _read_capture_text(capture, raw_directory=self.raw_directory)
                )
            except ParserError as exc:
                warnings.append(str(exc))
                drift_codes.append("json-parse-failed")
                continue
            schema = raw.get("schema")
            if schema != SYNTHETIC_FIXTURE_SCHEMA:
                drift_codes.append("unknown-schema")
                warnings.append(f"unexpected schema {schema!r}")
                continue
            payloads.append(_translate_fixture_bundle(raw))
        if not payloads and captures:
            drift_codes.append("no-example-payload")
        return ExtractionResult(
            profile_id=self.profile_id,
            verified=self.verified,
            adapter_contract_payloads=tuple(payloads),
            drift_codes=tuple(sorted(set(drift_codes))),
            warnings=tuple(warnings),
        )


def _translate_fixture_bundle(raw: dict[str, Any]) -> dict[str, object]:
    events: list[dict[str, object]] = []
    for item in raw.get("events", []):
        if not isinstance(item, dict):
            continue
        events.append(
            {
                "eventId": item.get("event_id") or item.get("eventId"),
                "competitionId": item.get("competition_id") or item.get("competitionId"),
                "competitionName": item.get("competition_name") or item.get("competitionName"),
                "sportCode": _sport_code(item.get("sport") or item.get("sportCode")),
                "state": "PREMATCH",
                "startTimeUtc": item.get("start_time_utc") or item.get("startTimeUtc"),
                "sourcePageRouteId": item.get("source_page_route_id")
                or item.get("sourcePageRouteId"),
                "participants": [
                    {
                        "participantId": p.get("participant_id") or p.get("participantId"),
                        "name": p.get("name"),
                        "role": (p.get("role") or "HOME").upper(),
                        "normalizedName": p.get("normalized_name") or p.get("normalizedName"),
                    }
                    for p in item.get("participants", [])
                    if isinstance(p, dict)
                ],
                "markets": [
                    _translate_market(market)
                    for market in item.get("markets", [])
                    if isinstance(market, dict)
                ],
            }
        )
    unsupported = raw.get("unknown_markets") or raw.get("unsupportedMarkets") or []
    return {
        "schema": ADAPTER_CONTRACT_SCHEMA,
        "events": events,
        "unsupportedMarkets": list(unsupported),
    }


def _read_capture_text(
    capture: BookmakerRawCapture,
    *,
    raw_directory: Path | None,
) -> str:
    if raw_directory is not None:
        return resolve_raw_path(raw_directory, capture.relative_path).read_text(encoding="utf-8")
    msg = "example profile requires raw_directory to resolve capture bytes in tests"
    raise ParserError(msg)


EXAMPLE_BETCLIC_SYNTHETIC_PROFILE = ExampleBetclicSyntheticExtractionProfile()

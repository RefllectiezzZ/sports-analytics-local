"""Betano example/synthetic extraction profile tests (not provider-native)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sports_analytics.sources.betano.catalog import ADAPTER_VERSION, PROVIDER_ID
from sports_analytics.sources.betclic.catalog import PROVIDER_ID as BETCLIC_PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.adapter_contract import (
    ADAPTER_CONTRACT_SCHEMA,
    parse_adapter_contract_payloads,
)
from sports_analytics.sources.bookmaker_extraction.example_betano import (
    EXAMPLE_BETANO_SYNTHETIC_PROFILE,
    ExampleBetanoSyntheticExtractionProfile,
)
from sports_analytics.sources.bookmaker_extraction.pipeline import apply_extraction_profile
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "betano" / "football.json"


def _browser() -> BrowserAcquisitionResult:
    return BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="cycle-example",
        observed_at_utc=OBSERVED,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )


def _stored_capture(tmp_path: Path, *, content: str):
    store = BookmakerRawCaptureStore(tmp_path)
    return store.store_text(
        source_name=PROVIDER_ID,
        capture_kind="provider-json",
        content=content,
        retrieved_at=OBSERVED,
        extension="json",
        source_url="https://www.betano.pt/test",
    )


def test_example_profile_positive_translation(tmp_path: Path) -> None:
    profile = ExampleBetanoSyntheticExtractionProfile()
    profile.raw_directory = tmp_path
    payload = FIXTURE.read_text(encoding="utf-8")
    capture = _stored_capture(tmp_path, content=payload)
    result = profile.extract(browser_result=_browser(), captures=(capture,))
    assert result.verified is False
    assert len(result.adapter_contract_payloads) == 1
    assert result.adapter_contract_payloads[0]["schema"] == ADAPTER_CONTRACT_SCHEMA
    bundle = apply_extraction_profile(
        profile=profile,
        browser_result=_browser(),
        captures=(capture,),
        adapter_version=ADAPTER_VERSION,
        parser_version="betano-pt-parser-v1",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    assert "unverified-extraction-profile" in bundle.drift_codes
    assert len(bundle.events) == 1


def test_example_profile_rejects_unknown_schema(tmp_path: Path) -> None:
    profile = ExampleBetanoSyntheticExtractionProfile()
    profile.raw_directory = tmp_path
    bad = json.dumps({"schema": "unknown-envelope-v9", "events": []})
    result = profile.extract(
        browser_result=_browser(),
        captures=(_stored_capture(tmp_path, content=bad),),
    )
    assert "unknown-schema" in result.drift_codes
    assert result.adapter_contract_payloads == ()


def test_adapter_contract_fail_closed_on_missing_state() -> None:
    payload = {
        "schema": ADAPTER_CONTRACT_SCHEMA,
        "events": [
            {
                "eventId": "e1",
                "competitionId": "c1",
                "sportCode": "FOOT",
                "startTimeUtc": "2026-07-27T15:00:00Z",
                "participants": [],
                "markets": [],
            }
        ],
    }
    bundle = parse_adapter_contract_payloads(
        [payload],
        provider_id=PROVIDER_ID,
        adapter_version=ADAPTER_VERSION,
        acquisition_cycle_id="cycle-strict",
        observed_at_utc=OBSERVED,
        sport="football",
        parser_version="test-parser",
    )
    assert bundle.events == ()


def test_production_profile_defaults_to_none() -> None:
    betano_profile = get_verified_extraction_profile(PROVIDER_ID)
    assert betano_profile is not None
    assert betano_profile.verified is True
    assert get_verified_extraction_profile(BETCLIC_PROVIDER_ID) is None
    assert EXAMPLE_BETANO_SYNTHETIC_PROFILE.verified is False

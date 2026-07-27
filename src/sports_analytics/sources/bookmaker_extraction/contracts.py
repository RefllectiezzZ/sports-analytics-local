"""Contracts for provider extraction profiles and the internal adapter envelope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
from sports_analytics.sources.raw_capture import BookmakerRawCapture

ADAPTER_CONTRACT_SCHEMA: str = "bookmaker-adapter-contract-v1"


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Outcome of applying one extraction profile to captured browser evidence."""

    profile_id: str
    verified: bool
    adapter_contract_payloads: tuple[dict[str, object], ...]
    drift_codes: tuple[str, ...]
    warnings: tuple[str, ...]


class ExtractionProfile(Protocol):
    """Translate sanitized provider captures into internal adapter-contract payloads."""

    @property
    def profile_id(self) -> str: ...

    @property
    def verified(self) -> bool: ...

    @property
    def provider_id(self) -> str: ...

    def extract(
        self,
        *,
        browser_result: BrowserAcquisitionResult,
        captures: tuple[BookmakerRawCapture, ...],
    ) -> ExtractionResult: ...

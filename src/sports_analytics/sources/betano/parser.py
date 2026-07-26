"""Production Betano parser entrypoint (native offering only)."""

from __future__ import annotations

from sports_analytics.sources.betano.native_parser import (
    parse_betano_acquisition,
    parse_betano_native_payloads,
)

__all__ = ["parse_betano_acquisition", "parse_betano_native_payloads"]

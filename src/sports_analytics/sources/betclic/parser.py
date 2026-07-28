"""Production Betclic parser entrypoint (native offering only)."""

from __future__ import annotations

from sports_analytics.sources.betclic.native_parser import (
    parse_betclic_acquisition,
    parse_betclic_native_payloads,
)

__all__ = ["parse_betclic_acquisition", "parse_betclic_native_payloads"]

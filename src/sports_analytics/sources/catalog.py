"""Static top-level source catalog."""

from __future__ import annotations

from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK


def list_source_names() -> tuple[str, ...]:
    """Return registered external source identifiers in deterministic order."""
    return (SOURCE_FOOTBALL_DATA_CO_UK,)

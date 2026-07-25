"""Static top-level source catalog with roles and capabilities.

Only implemented adapters appear here. Bookmaker/current-odds adapters such as
Betclic and Betano are intentionally absent because no such adapter exists yet.
"""

from __future__ import annotations

from typing import Final

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.contracts import (
    SourceCapability,
    SourceDescriptor,
    SourceRole,
)
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.identifiers import SPORT_FOOTBALL

FOOTBALL_DATA_ADAPTER_VERSION: Final[str] = "football-data-co-uk-adapter-v1"

_FOOTBALL_DATA_DESCRIPTOR: Final[SourceDescriptor] = SourceDescriptor(
    source_id=SOURCE_FOOTBALL_DATA_CO_UK,
    display_name="Football-Data.co.uk",
    role=SourceRole.HISTORICAL_DATA,
    adapter_version=FOOTBALL_DATA_ADAPTER_VERSION,
    capabilities=frozenset(
        {
            SourceCapability.HISTORICAL_RESULTS,
            SourceCapability.HISTORICAL_STATISTICS,
            SourceCapability.HISTORICAL_ODDS,
        }
    ),
    supported_sports=(SPORT_FOOTBALL,),
    supported_scopes=("eng-premier-league", "prt-primeira-liga"),
    requires_network=True,
    notes=(
        "Historical season CSV ingestion adapter. Publishes historical results, "
        "post-match statistics, and historical 1X2 quotes. It provides no current "
        "fixtures, no current bookmaker prices, and no settlement feed."
    ),
)

_DESCRIPTORS: Final[tuple[SourceDescriptor, ...]] = tuple(
    sorted((_FOOTBALL_DATA_DESCRIPTOR,), key=lambda item: item.source_id)
)


def list_source_descriptors() -> tuple[SourceDescriptor, ...]:
    """Return implemented source descriptors in deterministic ``source_id`` order."""
    return _DESCRIPTORS


def list_source_names() -> tuple[str, ...]:
    """Return registered external source identifiers in deterministic order."""
    return tuple(descriptor.source_id for descriptor in _DESCRIPTORS)


def get_source_descriptor(source_id: str) -> SourceDescriptor:
    """Resolve one implemented source descriptor by identifier."""
    for descriptor in _DESCRIPTORS:
        if descriptor.source_id == source_id:
            return descriptor
    msg = f"unsupported source_id: {source_id}"
    raise PermanentSourceError(msg)


def list_sources_with_capability(
    capability: SourceCapability | str,
) -> tuple[SourceDescriptor, ...]:
    """Return implemented sources providing ``capability`` in deterministic order."""
    return tuple(descriptor for descriptor in _DESCRIPTORS if descriptor.has_capability(capability))

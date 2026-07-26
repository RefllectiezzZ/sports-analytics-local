"""Static top-level source catalog with roles and capabilities.

Only implemented adapters appear here. Historical Football-Data.co.uk and the
Portugal bookmaker current-odds adapters (Betano, Betclic) are registered.
"""

from __future__ import annotations

from typing import Final

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.betano.catalog import (
    ADAPTER_VERSION as BETANO_ADAPTER_VERSION,
)
from sports_analytics.sources.betano.catalog import (
    ALLOWED_HOSTNAMES as BETANO_ALLOWED_HOSTNAMES,
)
from sports_analytics.sources.betano.catalog import (
    PROVIDER_ID as BETANO_PROVIDER_ID,
)
from sports_analytics.sources.betano.catalog import (
    SUPPORTED_MARKET_DEFINITION_IDS as BETANO_MARKET_DEFINITION_IDS,
)
from sports_analytics.sources.betclic.catalog import (
    ADAPTER_VERSION as BETCLIC_ADAPTER_VERSION,
)
from sports_analytics.sources.betclic.catalog import (
    ALLOWED_HOSTNAMES as BETCLIC_ALLOWED_HOSTNAMES,
)
from sports_analytics.sources.betclic.catalog import (
    PROVIDER_ID as BETCLIC_PROVIDER_ID,
)
from sports_analytics.sources.betclic.catalog import (
    SUPPORTED_MARKET_DEFINITION_IDS as BETCLIC_MARKET_DEFINITION_IDS,
)
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS
from sports_analytics.sources.contracts import (
    SourceCapability,
    SourceDescriptor,
    SourceRole,
)
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.identifiers import SPORT_FOOTBALL

FOOTBALL_DATA_ADAPTER_VERSION: Final[str] = "football-data-co-uk-adapter-v1"

_BOOKMAKER_CAPABILITIES: Final[frozenset[SourceCapability]] = frozenset(
    {
        SourceCapability.CURRENT_FIXTURES,
        SourceCapability.CURRENT_ODDS,
    }
)

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

_BETANO_DESCRIPTOR: Final[SourceDescriptor] = SourceDescriptor(
    source_id=BETANO_PROVIDER_ID,
    display_name="Betano Portugal",
    role=SourceRole.BOOKMAKER,
    adapter_version=BETANO_ADAPTER_VERSION,
    capabilities=_BOOKMAKER_CAPABILITIES,
    supported_sports=SUPPORTED_BOOKMAKER_SPORTS,
    supported_scopes=SUPPORTED_BOOKMAKER_SPORTS,
    requires_network=True,
    notes=(
        "Preferred Portugal bookmaker for pre-match fixtures and current odds. "
        "Visible-browser acquisition only; no live odds, bet placement, or cash-out."
    ),
    requires_browser=True,
    required_locale="pt-PT",
    allowed_hostnames=tuple(sorted(BETANO_ALLOWED_HOSTNAMES)),
    supported_market_definition_ids=tuple(sorted(BETANO_MARKET_DEFINITION_IDS)),
    pre_match_only=True,
    acquisition_status="implemented",
)

_BETCLIC_DESCRIPTOR: Final[SourceDescriptor] = SourceDescriptor(
    source_id=BETCLIC_PROVIDER_ID,
    display_name="Betclic Portugal",
    role=SourceRole.BOOKMAKER,
    adapter_version=BETCLIC_ADAPTER_VERSION,
    capabilities=_BOOKMAKER_CAPABILITIES,
    supported_sports=SUPPORTED_BOOKMAKER_SPORTS,
    supported_scopes=SUPPORTED_BOOKMAKER_SPORTS,
    requires_network=True,
    notes=(
        "Comparison and first-fallback Portugal bookmaker for pre-match fixtures "
        "and current odds. Visible-browser acquisition only; no live odds, bet "
        "placement, or cash-out."
    ),
    requires_browser=True,
    required_locale="pt-PT",
    allowed_hostnames=tuple(sorted(BETCLIC_ALLOWED_HOSTNAMES)),
    supported_market_definition_ids=tuple(sorted(BETCLIC_MARKET_DEFINITION_IDS)),
    pre_match_only=True,
    acquisition_status="implemented",
)

_DESCRIPTORS: Final[tuple[SourceDescriptor, ...]] = tuple(
    sorted(
        (_FOOTBALL_DATA_DESCRIPTOR, _BETANO_DESCRIPTOR, _BETCLIC_DESCRIPTOR),
        key=lambda item: item.source_id,
    )
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

"""Source roles and capabilities.

Different sources serve different purposes. Football-Data.co.uk is a historical
CSV ingestion source; future bookmaker adapters (for example Betclic or Betano)
would be current-market sources. Callers must be able to ask what a source can
actually do instead of assuming one behaviour for every source.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sports_analytics.core.exceptions import PermanentSourceError, RepositoryError
from sports_analytics.data.types import validate_identifier


class SourceRole(StrEnum):
    """Primary purpose a source serves in the platform."""

    HISTORICAL_DATA = "historical-data"
    BOOKMAKER = "bookmaker"
    FIXTURE_CALENDAR = "fixture-calendar"
    RESULTS_FEED = "results-feed"


class SourceCapability(StrEnum):
    """A concrete capability a source adapter provides."""

    HISTORICAL_RESULTS = "historical-results"
    HISTORICAL_STATISTICS = "historical-statistics"
    HISTORICAL_ODDS = "historical-odds"
    CURRENT_FIXTURES = "current-fixtures"
    CURRENT_ODDS = "current-odds"
    SETTLEMENT_RESULTS = "settlement-results"


def parse_source_capability(value: str) -> SourceCapability:
    """Parse a capability identifier, raising a typed error for unknown values."""
    try:
        return SourceCapability(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in SourceCapability)
        msg = f"unknown source capability {value!r}; supported capabilities: {allowed}"
        raise PermanentSourceError(msg) from exc


def parse_source_role(value: str) -> SourceRole:
    """Parse a role identifier, raising a typed error for unknown values."""
    try:
        return SourceRole(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in SourceRole)
        msg = f"unknown source role {value!r}; supported roles: {allowed}"
        raise PermanentSourceError(msg) from exc


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """Static, immutable description of one implemented source adapter."""

    source_id: str
    display_name: str
    role: SourceRole
    adapter_version: str
    capabilities: frozenset[SourceCapability]
    supported_sports: tuple[str, ...]
    supported_scopes: tuple[str, ...]
    requires_network: bool
    notes: str
    requires_browser: bool = False
    required_locale: str | None = None
    allowed_hostnames: tuple[str, ...] = ()
    supported_market_definition_ids: tuple[str, ...] = ()
    pre_match_only: bool = False
    acquisition_status: str = "implemented"

    def __post_init__(self) -> None:
        try:
            validate_identifier(self.source_id, field_name="source_id")
            validate_identifier(self.adapter_version, field_name="adapter_version")
            for sport_code in self.supported_sports:
                validate_identifier(sport_code, field_name="supported_sport")
            for scope in self.supported_scopes:
                validate_identifier(scope, field_name="supported_scope")
            for market_id in self.supported_market_definition_ids:
                validate_identifier(market_id, field_name="supported_market_definition_id")
            validate_identifier(self.acquisition_status, field_name="acquisition_status")
            for hostname in self.allowed_hostnames:
                if not hostname or hostname != hostname.strip().lower():
                    msg = "allowed_hostnames must be lowercase non-empty hostnames"
                    raise PermanentSourceError(msg)
        except RepositoryError as exc:
            raise PermanentSourceError(str(exc)) from exc
        if not self.capabilities:
            msg = f"source {self.source_id} must declare at least one capability"
            raise PermanentSourceError(msg)
        if tuple(sorted(self.supported_sports)) != self.supported_sports:
            msg = f"source {self.source_id} supported_sports must be sorted"
            raise PermanentSourceError(msg)
        if tuple(sorted(self.supported_scopes)) != self.supported_scopes:
            msg = f"source {self.source_id} supported_scopes must be sorted"
            raise PermanentSourceError(msg)
        if tuple(sorted(self.allowed_hostnames)) != self.allowed_hostnames:
            msg = f"source {self.source_id} allowed_hostnames must be sorted"
            raise PermanentSourceError(msg)
        if tuple(sorted(self.supported_market_definition_ids)) != (
            self.supported_market_definition_ids
        ):
            msg = f"source {self.source_id} supported_market_definition_ids must be sorted"
            raise PermanentSourceError(msg)
        if self.required_locale is not None and not self.required_locale.strip():
            msg = f"source {self.source_id} required_locale must be non-empty when set"
            raise PermanentSourceError(msg)

    @property
    def capability_values(self) -> tuple[str, ...]:
        """Return declared capabilities as a deterministic sorted tuple."""
        return tuple(sorted(item.value for item in self.capabilities))

    def has_capability(self, capability: SourceCapability | str) -> bool:
        """Return whether the adapter provides ``capability``.

        Unknown capability strings raise ``PermanentSourceError`` instead of
        silently returning ``False``.
        """
        parsed = (
            capability
            if isinstance(capability, SourceCapability)
            else parse_source_capability(capability)
        )
        return parsed in self.capabilities

    def require_capability(self, capability: SourceCapability | str) -> None:
        """Raise ``PermanentSourceError`` when the adapter lacks ``capability``."""
        parsed = (
            capability
            if isinstance(capability, SourceCapability)
            else parse_source_capability(capability)
        )
        if parsed not in self.capabilities:
            msg = f"source {self.source_id} does not provide capability {parsed.value}"
            raise PermanentSourceError(msg)

    def supports_sport(self, sport_code: str) -> bool:
        """Return whether the adapter supports a sport code."""
        return sport_code in self.supported_sports

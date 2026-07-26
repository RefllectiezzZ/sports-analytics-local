"""Explicit dependency metadata contracts for trusted opportunity construction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sports_analytics.core.exceptions import OpportunityError


class DependencyMetadataProvenance(StrEnum):
    """Source of dependency metadata attached to one evaluated selection."""

    SYNTHETIC_CONTRACT = "synthetic-contract"
    TRUSTED_ANALYSIS_ARTIFACT = "trusted-analysis-artifact"


@dataclass(frozen=True, slots=True)
class SelectionDependencyMetadata:
    """Dependency metadata for one canonical selection within a complete market."""

    selection_id: str
    dependency_keys: frozenset[str]
    participant_ids: frozenset[str]
    dependency_metadata_complete: bool
    metadata_provenance: DependencyMetadataProvenance

    def __post_init__(self) -> None:
        if type(self.selection_id) is not str or not self.selection_id:
            raise OpportunityError("selection_id must be a non-empty string")
        if type(self.dependency_metadata_complete) is not bool:
            raise OpportunityError("dependency_metadata_complete must be boolean")
        if any(type(item) is not str or not item for item in self.dependency_keys):
            raise OpportunityError("dependency keys must be non-empty strings")
        if any(type(item) is not str or not item for item in self.participant_ids):
            raise OpportunityError("participant ids must be non-empty strings")
        if self.dependency_metadata_complete and (
            not self.dependency_keys or not self.participant_ids
        ):
            raise OpportunityError(
                "complete dependency metadata requires non-empty keys and participant ids"
            )


@dataclass(frozen=True, slots=True)
class MarketDependencyMetadata:
    """Selection-keyed dependency metadata for one prediction/quote market pair."""

    by_selection_id: dict[str, SelectionDependencyMetadata]

    def __post_init__(self) -> None:
        if not isinstance(self.by_selection_id, dict):
            raise OpportunityError("dependency metadata map must be a dict")
        for key, value in self.by_selection_id.items():
            if type(key) is not str or not key:
                raise OpportunityError("dependency metadata keys must be non-empty strings")
            if value.selection_id != key:
                raise OpportunityError("dependency metadata selection_id must match map key")

    def for_selection(self, selection_id: str) -> SelectionDependencyMetadata | None:
        return self.by_selection_id.get(selection_id)

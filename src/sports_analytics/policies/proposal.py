"""Complete immutable proposal policy for UI and operator publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, EvaluationError
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.proposals.football import SportCombinationMode

PROPOSAL_POLICY_TYPE: Final[str] = "published-proposal-policy"
PROPOSAL_POLICY_SCHEMA: Final[str] = "published-proposal-policy-v1"
_SPORTS: Final[frozenset[str]] = frozenset({"football", "basketball", "tennis"})


@dataclass(frozen=True, slots=True)
class PublishedProposalPolicy:
    """All proposal-builder limits with deterministic canonical ordering."""

    allowed_sports: tuple[str, ...] = ("football",)
    combination_mode: SportCombinationMode = SportCombinationMode.COMBINE_SELECTED_SPORTS
    provider_policy: tuple[str, ...] = ()
    minimum_legs: int = 2
    maximum_legs: int = 4
    minimum_total_odds: float = 1.2
    maximum_total_odds: float = 100.0
    minimum_edge: float = 0.02
    minimum_expected_value: float = 0.03
    maximum_uncertainty: float = 0.10
    schema_version: str = PROPOSAL_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if (
            not self.allowed_sports
            or self.allowed_sports != tuple(sorted(set(self.allowed_sports)))
            or not set(self.allowed_sports) <= _SPORTS
        ):
            raise EvaluationError("proposal policy sports must be canonical and ordered")
        if self.provider_policy != tuple(sorted(set(self.provider_policy))):
            raise EvaluationError("proposal provider policy must be canonical and ordered")
        if (
            type(self.minimum_legs) is not int
            or type(self.maximum_legs) is not int
            or self.minimum_legs < 1
            or self.maximum_legs < self.minimum_legs
        ):
            raise EvaluationError("proposal policy leg limits are invalid")
        if not 1.0 < self.minimum_total_odds <= self.maximum_total_odds:
            raise EvaluationError("proposal policy total-odds limits are invalid")
        for field in ("minimum_edge", "minimum_expected_value", "maximum_uncertainty"):
            value = getattr(self, field)
            if not 0.0 <= value <= 1.0:
                raise EvaluationError(f"{field} must lie in [0, 1]")

    @property
    def configuration_id(self) -> str:
        return content_addressed_id(
            identity_type=PROPOSAL_POLICY_SCHEMA,
            payload=self.to_json(),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "allowed_sports": list(self.allowed_sports),
            "combination_mode": self.combination_mode.value,
            "provider_policy": list(self.provider_policy),
            "minimum_legs": self.minimum_legs,
            "maximum_legs": self.maximum_legs,
            "minimum_total_odds": self.minimum_total_odds,
            "maximum_total_odds": self.maximum_total_odds,
            "minimum_edge": self.minimum_edge,
            "minimum_expected_value": self.minimum_expected_value,
            "maximum_uncertainty": self.maximum_uncertainty,
        }


def proposal_policy_template() -> str:
    policy = PublishedProposalPolicy()
    return (
        json.dumps(
            {"configuration_id": policy.configuration_id, **policy.to_json()},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def parse_proposal_policy(value: object) -> PublishedProposalPolicy:
    allowed_field_sets = (
        frozenset(
            {
                "schema_version",
                "allowed_sports",
                "combination_mode",
                "provider_policy",
                "minimum_legs",
                "maximum_legs",
                "minimum_total_odds",
                "maximum_total_odds",
                "minimum_edge",
                "minimum_expected_value",
                "maximum_uncertainty",
            }
        ),
        frozenset(
            {
                "configuration_id",
                "schema_version",
                "allowed_sports",
                "combination_mode",
                "provider_policy",
                "minimum_legs",
                "maximum_legs",
                "minimum_total_odds",
                "maximum_total_odds",
                "minimum_edge",
                "minimum_expected_value",
                "maximum_uncertainty",
            }
        ),
    )
    if not isinstance(value, dict) or frozenset(value) not in allowed_field_sets:
        raise EvaluationError("proposal policy fields are not exact")
    try:
        policy = PublishedProposalPolicy(
            allowed_sports=tuple(_string_list(value["allowed_sports"], "allowed_sports")),
            combination_mode=SportCombinationMode(str(value["combination_mode"])),
            provider_policy=tuple(_string_list(value["provider_policy"], "provider_policy")),
            minimum_legs=_integer(value["minimum_legs"], "minimum_legs"),
            maximum_legs=_integer(value["maximum_legs"], "maximum_legs"),
            minimum_total_odds=_number(value["minimum_total_odds"], "minimum_total_odds"),
            maximum_total_odds=_number(value["maximum_total_odds"], "maximum_total_odds"),
            minimum_edge=_number(value["minimum_edge"], "minimum_edge"),
            minimum_expected_value=_number(
                value["minimum_expected_value"],
                "minimum_expected_value",
            ),
            maximum_uncertainty=_number(
                value["maximum_uncertainty"],
                "maximum_uncertainty",
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationError("proposal policy contains invalid typed values") from exc
    supplied = value.get("configuration_id")
    if supplied is not None and supplied != policy.configuration_id:
        raise EvaluationError("proposal policy configuration identity is stale or forged")
    return policy


def publish_proposal_policy(
    *,
    root: Path,
    relative_directory: str,
    policy: PublishedProposalPolicy,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=PROPOSAL_POLICY_TYPE,
        schema_version=PROPOSAL_POLICY_SCHEMA,
        payload={"configuration_id": policy.configuration_id, **policy.to_json()},
    )


def load_published_proposal_policy(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> tuple[AnalyticalArtifact, PublishedProposalPolicy]:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=PROPOSAL_POLICY_TYPE,
        expected_schema_version=PROPOSAL_POLICY_SCHEMA,
        expected_checksum=expected_checksum,
    )
    try:
        policy = parse_proposal_policy(artifact.payload)
    except EvaluationError as exc:
        raise ArtifactError(str(exc)) from exc
    return artifact, policy


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise EvaluationError(f"{field} must be a string array")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise EvaluationError(f"{field} must be an integer")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvaluationError(f"{field} must be numeric")
    return float(value)

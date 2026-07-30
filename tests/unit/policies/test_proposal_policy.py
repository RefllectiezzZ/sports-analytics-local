from __future__ import annotations

import pytest

from sports_analytics.core.exceptions import EvaluationError
from sports_analytics.policies.proposal import (
    PublishedProposalPolicy,
    load_published_proposal_policy,
    parse_proposal_policy,
    proposal_policy_template,
    publish_proposal_policy,
)
from sports_analytics.proposals.football import SportCombinationMode


def test_policy_identity_reload_and_unselected_sport_boundary(tmp_path) -> None:
    policy = PublishedProposalPolicy(
        allowed_sports=("football", "tennis"),
        combination_mode=SportCombinationMode.SEPARATE_BY_SPORT,
        provider_policy=("provider-a",),
    )
    artifact = publish_proposal_policy(
        root=tmp_path,
        relative_directory=f"policies/{policy.configuration_id}",
        policy=policy,
    )
    _, loaded = load_published_proposal_policy(
        root=tmp_path,
        relative_directory=f"policies/{policy.configuration_id}",
        expected_checksum=artifact.checksum_sha256,
    )
    assert loaded == policy
    assert "basketball" not in loaded.allowed_sports
    assert loaded.combination_mode is SportCombinationMode.SEPARATE_BY_SPORT


def test_policy_template_is_strict_and_canonical() -> None:
    import json

    policy = parse_proposal_policy(json.loads(proposal_policy_template()))
    assert policy.allowed_sports == ("football",)
    with pytest.raises(EvaluationError):
        PublishedProposalPolicy(allowed_sports=("tennis", "football"))

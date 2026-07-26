"""Trusted combination construction from verified analysis artifacts."""

from __future__ import annotations

from sports_analytics.artifact_schemas import validate_cross_dataset_integrity
from sports_analytics.artifacts import load_typed_analytical_artifact
from sports_analytics.combinations.builder import CombinationBuildResult, build_combinations
from sports_analytics.combinations.contracts import CombinationRules
from sports_analytics.combinations.evidence import CombinationEvidenceMode
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.opportunities.contracts import Opportunity
from sports_analytics.opportunities.identity import verify_opportunity_identity
from sports_analytics.services.analysis import ANALYSIS_ARTIFACT_SCHEMA


def load_eligible_opportunities_from_analysis_artifact(
    *,
    paths: RuntimePaths,
    relative_directory: str,
    expected_checksum: str,
    filter_config_id: str | None = None,
) -> tuple[Opportunity, ...]:
    """Load eligible opportunities with verified lineage from one analysis artifact."""
    artifact = load_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative_directory,
        expected_kind="analysis",
        expected_schema_version=ANALYSIS_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    datasets = {dataset.name: dataset.rows for dataset in artifact.datasets}
    validate_cross_dataset_integrity(datasets)
    opportunity_rows = {row["opportunity_id"]: row for row in datasets["opportunities"]}
    decisions = datasets["opportunity_decisions"]
    eligible_ids = {
        row["opportunity_id"]
        for row in decisions
        if row.get("eligible") is True
        and (filter_config_id is None or row.get("filter_config_id") == filter_config_id)
    }
    opportunities: list[Opportunity] = []
    for opportunity_id in sorted(eligible_ids, key=str):
        row = opportunity_rows.get(opportunity_id)
        if row is None:
            raise ArtifactError(f"eligible opportunity row is missing: {opportunity_id}")
        opportunity = _opportunity_from_row(dict(row))
        verify_opportunity_identity(opportunity)
        opportunities.append(opportunity)
    return tuple(opportunities)


def build_combinations_from_analysis_artifact(
    *,
    paths: RuntimePaths,
    relative_directory: str,
    expected_checksum: str,
    rules: CombinationRules,
    filter_config_id: str | None = None,
) -> CombinationBuildResult:
    """Build trusted combinations from verified analysis artifact opportunities."""
    opportunities = load_eligible_opportunities_from_analysis_artifact(
        paths=paths,
        relative_directory=relative_directory,
        expected_checksum=expected_checksum,
        filter_config_id=filter_config_id,
    )
    return build_combinations(
        opportunities,
        rules=rules,
        evidence_mode=CombinationEvidenceMode.TRUSTED_VERIFIED,
    )


def _opportunity_from_row(row: dict[str, object]) -> Opportunity:
    from sports_analytics.services.analysis_json import _opportunity

    return _opportunity(row)

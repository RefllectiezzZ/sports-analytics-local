"""Typed opportunity discovery filters and ranking."""

from sports_analytics.opportunities.contracts import (
    Opportunity,
    OpportunityDecision,
    OpportunityFilter,
    OpportunityRankingMode,
    OpportunityRejection,
    OpportunitySearchResult,
    RejectionCode,
    filter_and_rank_opportunities,
    opportunities_from_evaluation,
)

__all__ = [
    "Opportunity",
    "OpportunityDecision",
    "OpportunityFilter",
    "OpportunityRankingMode",
    "OpportunityRejection",
    "OpportunitySearchResult",
    "RejectionCode",
    "filter_and_rank_opportunities",
    "opportunities_from_evaluation",
]

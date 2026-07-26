"""Explicit evidence boundaries for combination construction."""

from __future__ import annotations

from enum import StrEnum


class CombinationEvidenceMode(StrEnum):
    """Whether combination legs are trusted verified evidence or synthetic contract input."""

    SYNTHETIC_CONTRACT = "synthetic-contract"
    TRUSTED_VERIFIED = "trusted-verified"


SYNTHETIC_COMBINATION_EVIDENCE_LABEL = "synthetic-contract-non-production"

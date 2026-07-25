"""Content-addressed artifact identity helpers."""

from __future__ import annotations

import hashlib

from sports_analytics.core.exceptions import FeatureError, ModelError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.data.types import JsonValue


def content_addressed_id(*, identity_type: str, payload: dict[str, JsonValue]) -> str:
    """Derive a deterministic SHA-256 artifact identifier from canonical JSON."""
    if not identity_type:
        msg = "identity_type must be non-empty"
        raise FeatureError(msg)
    canonical = dumps_canonical_json({"identity_type": identity_type, **payload})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_artifact_id_override(
    *,
    override: str | None,
    derived: str,
    artifact_kind: str,
) -> str:
    """Return the artifact ID, rejecting overrides that disagree with derived identity."""
    if override is None:
        return derived
    if override != derived:
        msg = (
            f"{artifact_kind} artifact_id override does not match content-addressed identity: "
            f"override={override} derived={derived}"
        )
        raise ModelError(msg)
    return derived

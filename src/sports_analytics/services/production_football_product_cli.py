"""Strict JSON adapter for production football inference."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

from sports_analytics.bookmakers.operator_quotes import parse_operator_quote_json
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.data.codec import dumps_canonical_json, parse_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.services.production_football_product import (
    ProductionFootballProductRequest,
    run_and_publish_production_football_product,
)


def run_production_football_product_json(
    *,
    path_text: str,
    connection: sqlite3.Connection,
    exports_root: Path,
    model_root: Path,
) -> dict[str, JsonValue]:
    try:
        document = json.loads(Path(path_text.replace("\\", "/")).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("cannot read production football product JSON") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError("production football product JSON is malformed") from exc
    return run_production_football_product_document(
        document=document,
        connection=connection,
        exports_root=exports_root,
        model_root=model_root,
    )


def run_production_football_product_document(
    *,
    document: object,
    connection: sqlite3.Connection,
    exports_root: Path,
    model_root: Path,
) -> dict[str, JsonValue]:
    if not isinstance(document, dict) or set(document) != {
        "relative_root",
        "evaluated_at_utc",
        "competition_id",
        "market_key",
        "upcoming_event_artifact",
        "participant_registry_artifact",
        "proposal_policy_artifact",
        "registered_provider_ids",
        "operator_quotes",
        "player_context_artifact",
        "economic_evidence_artifact",
    }:
        raise ConfigurationError("production football product JSON fields are not exact")
    events = _reference(document["upcoming_event_artifact"], "upcoming_event_artifact", True)
    participants = _reference(
        document["participant_registry_artifact"], "participant_registry_artifact", True
    )
    policy = _reference(document["proposal_policy_artifact"], "proposal_policy_artifact", False)
    player_raw = document["player_context_artifact"]
    player = (
        None if player_raw is None else _reference(player_raw, "player_context_artifact", False)
    )
    evidence_raw = document["economic_evidence_artifact"]
    evidence = (
        None
        if evidence_raw is None
        else _reference(evidence_raw, "economic_evidence_artifact", True)
    )
    providers_raw = document["registered_provider_ids"]
    if not isinstance(providers_raw, list) or any(
        type(item) is not str or not item for item in providers_raw
    ):
        raise ConfigurationError("registered_provider_ids must be a string array")
    quotes_raw = document["operator_quotes"]
    if not isinstance(quotes_raw, list):
        raise ConfigurationError("operator_quotes must be an array")
    quotes = (
        ()
        if not quotes_raw
        else parse_operator_quote_json(
            dumps_canonical_json(cast(JsonValue, quotes_raw)).encode("utf-8")
        )
    )
    published = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports_root,
        model_root=model_root,
        request=ProductionFootballProductRequest(
            upcoming_event_relative_directory=events["relative_directory"],
            upcoming_event_artifact_id=events["artifact_id"],
            upcoming_event_checksum_sha256=events["checksum_sha256"],
            participant_registry_relative_directory=participants["relative_directory"],
            participant_registry_artifact_id=participants["artifact_id"],
            participant_registry_checksum_sha256=participants["checksum_sha256"],
            competition_id=_text(document["competition_id"], "competition_id"),
            market_key=_text(document["market_key"], "market_key"),
            evaluated_at_utc=_timestamp(document["evaluated_at_utc"]),
            relative_root=_text(document["relative_root"], "relative_root"),
            proposal_policy_relative_directory=policy["relative_directory"],
            proposal_policy_checksum_sha256=policy["checksum_sha256"],
            operator_quotes=quotes,
            registered_provider_ids=frozenset(str(item) for item in providers_raw),
            player_context_relative_directory=(
                None if player is None else player["relative_directory"]
            ),
            player_context_checksum_sha256=(None if player is None else player["checksum_sha256"]),
            economic_evidence_relative_directory=(
                None if evidence is None else evidence["relative_directory"]
            ),
            economic_evidence_artifact_id=(None if evidence is None else evidence["artifact_id"]),
            economic_evidence_checksum_sha256=(
                None if evidence is None else evidence["checksum_sha256"]
            ),
        ),
    )
    return {
        "state": (
            "no-production-champion"
            if not published.probability_artifacts
            else "fair-odds-only"
            if published.quote_catalogue is None
            else "production-inference-complete"
        ),
        "read_model_artifact_id": published.read_model_artifact.artifact_id,
        "probability_artifact_ids": [item.artifact_id for item in published.probability_artifacts],
        "quote_artifact_id": (
            None if published.quote_artifact is None else published.quote_artifact.artifact_id
        ),
        "proposal_artifact_id": (
            None if published.proposal_artifact is None else published.proposal_artifact.artifact_id
        ),
        "accepted_single_count": (
            0
            if published.proposals is None
            else sum(item.accepted for item in published.proposals.decisions)
        ),
        "accumulator_count": (
            0 if published.proposals is None else len(published.proposals.accumulators)
        ),
        "placement_state": "manual-only",
        "bookmaker_network_access": False,
    }


def _reference(value: object, field: str, require_id: bool) -> dict[str, str]:
    fields = {"relative_directory", "checksum_sha256"} | ({"artifact_id"} if require_id else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ConfigurationError(f"{field} fields are not exact")
    result = {key: _text(value[key], f"{field}.{key}") for key in fields}
    if not require_id:
        result["artifact_id"] = ""
    return result


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfigurationError(f"{field} must be non-empty trimmed text")
    return value


def _timestamp(value: object) -> datetime:
    text = _text(value, "evaluated_at_utc")
    if not text.endswith("Z"):
        raise ConfigurationError("evaluated_at_utc must be canonical UTC")
    try:
        return parse_utc_timestamp(text)
    except Exception as exc:
        raise ConfigurationError("evaluated_at_utc is invalid") from exc

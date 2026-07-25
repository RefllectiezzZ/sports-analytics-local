"""Trusted historical analysis publication with verified model and feature artifacts."""

from __future__ import annotations

from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.data.types import JsonValue
from sports_analytics.predictions.provenance import PredictionProvenance
from sports_analytics.predictions.replay import derive_historical_replay_cutoff_utc
from sports_analytics.predictions.service import (
    VerifiedPredictionRequest,
    generate_verified_football_1x2_prediction,
)
from sports_analytics.services.analysis import (
    ANALYSIS_ARTIFACT_SCHEMA,
    AnalysisMarketInput,
    AnalysisPublicationRequest,
    publish_analysis_artifact,
)
from sports_analytics.services.analysis_json import (
    _array,
    _complete_quote_from_payload,
    _datetime,
    _enum,
    _filters,
    _mapping,
    _optional_string,
    _rules,
    _string,
)
from sports_analytics.value.contracts import QuoteEvaluationMode


def publish_historical_analysis_with_paths(
    payload: object,
    *,
    paths: RuntimePaths,
    model_relative_path: str,
    model_checksum_sha256: str,
    feature_relative_directory: str,
    feature_manifest_checksum_sha256: str,
) -> dict[str, JsonValue]:
    """Build and publish one historical-replay analysis from verified artifacts only."""
    document = _mapping(payload, "historical analysis publication request")
    if not model_relative_path or not feature_relative_directory:
        raise ConfigurationError("model and feature artifact paths must be explicit")
    if not model_checksum_sha256 or not feature_manifest_checksum_sha256:
        raise ConfigurationError("model and feature checksums must be explicit")
    markets_payload = document.get("markets")
    if markets_payload is None:
        markets: tuple[AnalysisMarketInput, ...] = (
            _historical_market_input(
                _mapping(
                    {
                        "canonical_event_id": document.get("canonical_event_id"),
                        "event_start_utc": document.get("event_start_utc"),
                        "quote": document.get("quote"),
                    },
                    "market",
                ),
                paths=paths,
                model_relative_path=model_relative_path,
                model_checksum_sha256=model_checksum_sha256,
                feature_relative_directory=feature_relative_directory,
                feature_manifest_checksum_sha256=feature_manifest_checksum_sha256,
            ),
        )
    else:
        markets = tuple(
            _historical_market_input(
                _mapping(item, "market"),
                paths=paths,
                model_relative_path=model_relative_path,
                model_checksum_sha256=model_checksum_sha256,
                feature_relative_directory=feature_relative_directory,
                feature_manifest_checksum_sha256=feature_manifest_checksum_sha256,
            )
            for item in _array(document, "markets")
        )
    mode = _enum(
        QuoteEvaluationMode,
        document.get("mode", QuoteEvaluationMode.LIVE_SAFE.value),
        "mode",
    )
    filters = _filters(_mapping(document.get("filters", {}), "filters"))
    rules_payload = document.get("combination_rules")
    combination_rules = (
        None if rules_payload is None else _rules(_mapping(rules_payload, "combination_rules"))
    )
    relative_directory = _optional_string(document.get("relative_directory"))
    published = publish_analysis_artifact(
        paths=paths,
        request=AnalysisPublicationRequest(
            markets=markets,
            mode=mode,
            filters=filters,
            combination_rules=combination_rules,
            provenance=PredictionProvenance.HISTORICAL_REPLAY,
            relative_directory=relative_directory,
        ),
    )
    return {
        "artifact_id": published.artifact_id,
        "checksum_sha256": published.checksum_sha256,
        "relative_directory": published.relative_directory,
        "analysis_run_id": published.analysis_run_id,
        "schema_version": ANALYSIS_ARTIFACT_SCHEMA,
        "provenance": PredictionProvenance.HISTORICAL_REPLAY.value,
    }


def _historical_market_input(
    document: dict[str, object],
    *,
    paths: RuntimePaths,
    model_relative_path: str,
    model_checksum_sha256: str,
    feature_relative_directory: str,
    feature_manifest_checksum_sha256: str,
) -> AnalysisMarketInput:
    if document.get("prediction") is not None:
        raise ConfigurationError(
            "historical analysis must not accept caller-supplied prediction payloads"
        )
    canonical_event_id = _string(document, "canonical_event_id")
    event_start_utc = _datetime(document, "event_start_utc")
    prediction = generate_verified_football_1x2_prediction(
        paths=paths,
        request=VerifiedPredictionRequest(
            model_relative_path=model_relative_path,
            model_checksum_sha256=model_checksum_sha256,
            feature_relative_directory=feature_relative_directory,
            feature_manifest_checksum_sha256=feature_manifest_checksum_sha256,
            canonical_event_id=canonical_event_id,
            event_start_utc=event_start_utc,
            predicted_at_utc=derive_historical_replay_cutoff_utc(event_start_utc),
            provenance=PredictionProvenance.HISTORICAL_REPLAY,
        ),
    )
    quote = _complete_quote_from_payload(_mapping(document.get("quote"), "quote"))
    if quote.canonical_event_id != canonical_event_id:
        raise ConfigurationError("quote canonical_event_id must match market canonical_event_id")
    return AnalysisMarketInput(prediction=prediction, quote=quote, dependency_metadata=None)

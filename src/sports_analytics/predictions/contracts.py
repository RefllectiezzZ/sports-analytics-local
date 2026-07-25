"""Immutable sport-agnostic probability prediction contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from sports_analytics.core.exceptions import PredictionError, RepositoryError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_identifier, validate_sha256_checksum
from sports_analytics.features.contracts import PROBABILITY_SUM_TOLERANCE
from sports_analytics.markets.contracts import MarketDefinition, MarketSelection
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc

PREDICTION_SCHEMA_VERSION: Final[str] = "market-prediction-v1"


@dataclass(frozen=True, slots=True, order=True)
class CanonicalSelectionIdentity:
    """Source-independent identity of one canonical market selection."""

    sport_code: str
    market_family: str
    market_key: str
    market_period: str
    participant_scope: str
    canonical_participant_id: str | None
    line_type: str
    line_value: Decimal | None
    outcome_key: str

    def __post_init__(self) -> None:
        try:
            self.to_selection()
        except Exception as exc:
            raise PredictionError(f"invalid canonical selection identity: {exc}") from exc

    @classmethod
    def from_selection(cls, selection: MarketSelection) -> CanonicalSelectionIdentity:
        """Drop source market/selection identifiers from a canonical selection."""
        definition = selection.definition
        return cls(
            sport_code=definition.sport_code,
            market_family=definition.market_family,
            market_key=definition.market_key,
            market_period=definition.market_period,
            participant_scope=definition.participant_scope,
            canonical_participant_id=definition.canonical_participant_id,
            line_type=definition.line_type,
            line_value=definition.line_value,
            outcome_key=selection.outcome_key,
        )

    def to_selection(self) -> MarketSelection:
        """Return the shared canonical market contract without source identity."""
        return MarketSelection(
            definition=MarketDefinition(
                sport_code=self.sport_code,
                market_family=self.market_family,
                market_key=self.market_key,
                market_period=self.market_period,
                participant_scope=self.participant_scope,
                line_type=self.line_type,
                line_value=self.line_value,
                canonical_participant_id=self.canonical_participant_id,
            ),
            outcome_key=self.outcome_key,
        )

    def identity_payload(self) -> dict[str, JsonValue]:
        """Return canonical fields used by content-addressed identities."""
        return {
            "sport_code": self.sport_code,
            "market_family": self.market_family,
            "market_key": self.market_key,
            "market_period": self.market_period,
            "participant_scope": self.participant_scope,
            "canonical_participant_id": self.canonical_participant_id,
            "line_type": self.line_type,
            "line_value": None if self.line_value is None else format(self.line_value, "f"),
            "outcome_key": self.outcome_key,
        }

    @property
    def selection_id(self) -> str:
        """Return a deterministic content-addressed selection identifier."""
        return content_addressed_id(
            identity_type="canonical-market-selection-v1",
            payload=self.identity_payload(),
        )

    @property
    def market_identity(self) -> tuple[str, str, str, str, str, str | None, str, Decimal | None]:
        """Return all canonical market dimensions except the outcome."""
        return (
            self.sport_code,
            self.market_family,
            self.market_key,
            self.market_period,
            self.participant_scope,
            self.canonical_participant_id,
            self.line_type,
            self.line_value,
        )


@dataclass(frozen=True, slots=True)
class PredictionInputSnapshot:
    """One immutable input snapshot inherited through feature/model lineage."""

    snapshot_id: str
    manifest_checksum_sha256: str
    schema_version: str
    source_name: str

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "schema_version", "source_name"):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise PredictionError(f"{field_name} must be a non-empty string")
        try:
            validate_sha256_checksum(self.manifest_checksum_sha256)
        except RepositoryError as exc:
            raise PredictionError("input snapshot checksum is malformed") from exc


@dataclass(frozen=True, slots=True)
class PredictionQualityFlags:
    """Explicit production-readiness claims attached to one prediction."""

    calibrated: bool = False
    model_artifact_verified: bool = False
    feature_artifact_verified: bool = False
    sufficient_history: bool = False
    data_quality_passed: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "calibrated",
            "model_artifact_verified",
            "feature_artifact_verified",
            "sufficient_history",
            "data_quality_passed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise PredictionError(f"{field_name} must be boolean")

    @property
    def production_eligible(self) -> bool:
        return all(
            (
                self.calibrated,
                self.model_artifact_verified,
                self.feature_artifact_verified,
                self.sufficient_history,
                self.data_quality_passed,
            )
        )


@dataclass(frozen=True, slots=True)
class PredictionLineage:
    """Verified feature/model lineage carried by every published prediction."""

    model_artifact_id: str
    model_checksum_sha256: str
    model_specification_version: str
    feature_artifact_id: str
    feature_manifest_checksum_sha256: str
    feature_specification_version: str
    feature_row_id: str
    trained_through_date: date
    calibrated_through_date: date
    input_snapshots: tuple[PredictionInputSnapshot, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "model_artifact_id",
            "model_specification_version",
            "feature_artifact_id",
            "feature_specification_version",
            "feature_row_id",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise PredictionError(f"{field_name} must be a non-empty string")
        try:
            validate_sha256_checksum(self.model_checksum_sha256)
            validate_sha256_checksum(self.feature_manifest_checksum_sha256)
        except RepositoryError as exc:
            raise PredictionError("prediction lineage checksum is malformed") from exc
        if (
            type(self.trained_through_date) is not date
            or type(self.calibrated_through_date) is not date
        ):
            raise PredictionError("prediction lineage cutoffs must be dates")
        if self.trained_through_date > self.calibrated_through_date:
            raise PredictionError("trained_through_date must not follow calibrated_through_date")
        snapshot_ids = tuple(item.snapshot_id for item in self.input_snapshots)
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise PredictionError("prediction lineage contains duplicate input snapshots")
        object.__setattr__(
            self,
            "input_snapshots",
            tuple(sorted(self.input_snapshots, key=lambda item: item.snapshot_id)),
        )


@dataclass(frozen=True, slots=True)
class SelectionProbability:
    """Probability assigned to one canonical selection."""

    selection: CanonicalSelectionIdentity
    probability: float

    def __post_init__(self) -> None:
        if isinstance(self.probability, bool) or not isinstance(self.probability, int | float):
            raise PredictionError("probability must be a number")
        value = float(self.probability)
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            raise PredictionError("probability must be finite and lie in [0, 1]")
        object.__setattr__(self, "probability", value)


@dataclass(frozen=True, slots=True)
class MarketPrediction:
    """A complete calibrated probability distribution for one event market."""

    prediction_id: str
    schema_version: str
    canonical_event_id: str
    event_start_utc: datetime
    predicted_at_utc: datetime
    feature_available_at_utc: datetime
    lineage: PredictionLineage
    probabilities: tuple[SelectionProbability, ...]
    ordered_selection_ids: tuple[str, ...] = ()
    quality: PredictionQualityFlags = PredictionQualityFlags()

    def __post_init__(self) -> None:
        if self.schema_version != PREDICTION_SCHEMA_VERSION:
            raise PredictionError(f"unsupported prediction schema version: {self.schema_version}")
        if type(self.canonical_event_id) is not str or not self.canonical_event_id.strip():
            raise PredictionError("canonical_event_id must be a non-empty string")
        object.__setattr__(
            self,
            "event_start_utc",
            _utc(self.event_start_utc, field_name="event_start_utc"),
        )
        object.__setattr__(
            self,
            "predicted_at_utc",
            _utc(self.predicted_at_utc, field_name="predicted_at_utc"),
        )
        object.__setattr__(
            self,
            "feature_available_at_utc",
            _utc(self.feature_available_at_utc, field_name="feature_available_at_utc"),
        )
        if self.quality.production_eligible:
            validate_live_prediction_timing(self)
        if not 2 <= len(self.probabilities) <= 4:
            raise PredictionError("prediction requires a declared 2, 3, or 4 outcome space")
        selection_ids = tuple(item.selection.selection_id for item in self.probabilities)
        if len(selection_ids) != len(set(selection_ids)):
            raise PredictionError("prediction contains duplicate canonical selections")
        market_identities = {item.selection.market_identity for item in self.probabilities}
        if len(market_identities) != 1:
            raise PredictionError("all prediction outcomes must belong to one canonical market")
        if not self.ordered_selection_ids:
            object.__setattr__(self, "ordered_selection_ids", selection_ids)
        elif self.ordered_selection_ids != selection_ids:
            raise PredictionError(
                "probabilities must exactly follow the declared ordered selection space"
            )
        total = math.fsum(item.probability for item in self.probabilities)
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise PredictionError("complete market probabilities must sum to one")
        if self.lineage.feature_row_id != self.canonical_event_id:
            raise PredictionError("feature_row_id must match canonical_event_id")
        expected_id = derive_prediction_id(
            canonical_event_id=self.canonical_event_id,
            event_start_utc=self.event_start_utc,
            predicted_at_utc=self.predicted_at_utc,
            feature_available_at_utc=self.feature_available_at_utc,
            lineage=self.lineage,
            probabilities=self.probabilities,
            ordered_selection_ids=self.ordered_selection_ids,
            quality=self.quality,
        )
        if self.prediction_id != expected_id:
            raise PredictionError("prediction_id does not match content-addressed identity")

    @property
    def production_eligible(self) -> bool:
        return self.quality.production_eligible

    @property
    def sport_code(self) -> str:
        return self.probabilities[0].selection.sport_code

    @property
    def market_key(self) -> str:
        return self.probabilities[0].selection.market_key

    def probability_for(self, selection: CanonicalSelectionIdentity) -> float:
        """Return the probability for an exact canonical selection."""
        for item in self.probabilities:
            if item.selection == selection:
                return item.probability
        raise PredictionError(
            f"selection is absent from complete prediction: {selection.selection_id}"
        )


def build_market_prediction(
    *,
    canonical_event_id: str,
    event_start_utc: datetime,
    predicted_at_utc: datetime,
    feature_available_at_utc: datetime,
    lineage: PredictionLineage,
    probabilities: tuple[SelectionProbability, ...],
    ordered_selection_space: tuple[CanonicalSelectionIdentity, ...] | None = None,
    quality: PredictionQualityFlags | None = None,
) -> MarketPrediction:
    """Build a prediction with its deterministic content-addressed identity."""
    if ordered_selection_space is None:
        normalized = tuple(sorted(probabilities, key=lambda item: item.selection.selection_id))
    else:
        declared_ids = tuple(item.selection_id for item in ordered_selection_space)
        by_id = {item.selection.selection_id: item for item in probabilities}
        if len(by_id) != len(probabilities) or set(by_id) != set(declared_ids):
            raise PredictionError(
                "probabilities must exactly cover the declared ordered selection space"
            )
        normalized = tuple(by_id[selection_id] for selection_id in declared_ids)
    selection_ids = tuple(item.selection.selection_id for item in normalized)
    resolved_quality = quality or PredictionQualityFlags()
    prediction_id = derive_prediction_id(
        canonical_event_id=canonical_event_id,
        event_start_utc=event_start_utc,
        predicted_at_utc=predicted_at_utc,
        feature_available_at_utc=feature_available_at_utc,
        lineage=lineage,
        probabilities=normalized,
        ordered_selection_ids=selection_ids,
        quality=resolved_quality,
    )
    return MarketPrediction(
        prediction_id=prediction_id,
        schema_version=PREDICTION_SCHEMA_VERSION,
        canonical_event_id=canonical_event_id,
        event_start_utc=event_start_utc,
        predicted_at_utc=predicted_at_utc,
        feature_available_at_utc=feature_available_at_utc,
        lineage=lineage,
        probabilities=normalized,
        ordered_selection_ids=selection_ids,
        quality=resolved_quality,
    )


def derive_prediction_id(
    *,
    canonical_event_id: str,
    event_start_utc: datetime,
    predicted_at_utc: datetime,
    feature_available_at_utc: datetime,
    lineage: PredictionLineage,
    probabilities: tuple[SelectionProbability, ...],
    ordered_selection_ids: tuple[str, ...] | None = None,
    quality: PredictionQualityFlags | None = None,
) -> str:
    """Derive identity from canonical selection probabilities and verified lineage."""
    declared_ids = ordered_selection_ids or tuple(
        item.selection.selection_id for item in probabilities
    )
    resolved_quality = quality or PredictionQualityFlags()
    return content_addressed_id(
        identity_type=PREDICTION_SCHEMA_VERSION,
        payload={
            "canonical_event_id": canonical_event_id,
            "event_start_utc": format_utc_timestamp(
                _utc(event_start_utc, field_name="event_start_utc")
            ),
            "predicted_at_utc": format_utc_timestamp(
                _utc(predicted_at_utc, field_name="predicted_at_utc")
            ),
            "feature_available_at_utc": format_utc_timestamp(
                _utc(feature_available_at_utc, field_name="feature_available_at_utc")
            ),
            "lineage": {
                "model_artifact_id": lineage.model_artifact_id,
                "model_checksum_sha256": lineage.model_checksum_sha256,
                "model_specification_version": lineage.model_specification_version,
                "feature_artifact_id": lineage.feature_artifact_id,
                "feature_manifest_checksum_sha256": lineage.feature_manifest_checksum_sha256,
                "feature_specification_version": lineage.feature_specification_version,
                "feature_row_id": lineage.feature_row_id,
                "trained_through_date": lineage.trained_through_date.isoformat(),
                "calibrated_through_date": lineage.calibrated_through_date.isoformat(),
                "input_snapshots": [
                    {
                        "snapshot_id": item.snapshot_id,
                        "manifest_checksum_sha256": item.manifest_checksum_sha256,
                        "schema_version": item.schema_version,
                        "source_name": item.source_name,
                    }
                    for item in lineage.input_snapshots
                ],
            },
            "ordered_selection_ids": list(declared_ids),
            "quality": {
                "calibrated": resolved_quality.calibrated,
                "model_artifact_verified": resolved_quality.model_artifact_verified,
                "feature_artifact_verified": resolved_quality.feature_artifact_verified,
                "sufficient_history": resolved_quality.sufficient_history,
                "data_quality_passed": resolved_quality.data_quality_passed,
            },
            "probabilities": [
                {
                    "selection": item.selection.identity_payload(),
                    "probability": item.probability,
                }
                for item in probabilities
            ],
        },
    )


def validate_live_prediction_timing(prediction: MarketPrediction) -> None:
    """Enforce information availability and strict pre-start live-style timing."""
    if prediction.feature_available_at_utc > prediction.predicted_at_utc:
        raise PredictionError("feature data was not available at prediction time")
    if prediction.predicted_at_utc >= prediction.event_start_utc:
        raise PredictionError("prediction time must be strictly before event start")
    event_date = prediction.event_start_utc.date()
    if prediction.lineage.trained_through_date >= event_date:
        raise PredictionError("model training history reaches prediction event date")
    if prediction.lineage.calibrated_through_date >= event_date:
        raise PredictionError("model calibration history reaches prediction event date")


def validate_prediction_identifier(value: str, *, field_name: str) -> str:
    """Expose shared strict identifier validation as a prediction-domain error."""
    try:
        return validate_identifier(value, field_name=field_name)
    except RepositoryError as exc:
        raise PredictionError(str(exc)) from exc


def _utc(value: datetime, *, field_name: str) -> datetime:
    try:
        return require_utc(value, field_name=field_name)
    except Exception as exc:
        if isinstance(exc, PredictionError):
            raise
        raise PredictionError(str(exc)) from exc

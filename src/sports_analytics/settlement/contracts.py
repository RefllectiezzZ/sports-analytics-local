"""Pure deterministic analytical settlement policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sports_analytics.core.exceptions import ResultError, SettlementError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.results.contracts import CanonicalResult, EventResultStatus
from sports_analytics.results.snapshots import VerifiedResultSnapshot
from sports_analytics.sports.contracts import require_utc

SETTLEMENT_VERSION: Final[str] = "analytical-settlement-v1"


class SettlementStatus(StrEnum):
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class SettlementPolicy:
    """Versioned flat-unit and combination void/push rules."""

    policy_id: str
    policy_version: str
    stake_units: Decimal = Decimal("1")
    void_push_leg_policy: str = "retain-leg-return-stake-v1"

    def __post_init__(self) -> None:
        if not self.policy_id or not self.policy_version:
            raise SettlementError("settlement policy identity must be non-empty")
        if self.stake_units != Decimal("1"):
            raise SettlementError("operational analytical settlement supports one flat unit only")
        if self.void_push_leg_policy != "retain-leg-return-stake-v1":
            raise SettlementError("unsupported combination void/push policy")


SETTLEMENT_POLICY_V1: Final[SettlementPolicy] = SettlementPolicy(
    policy_id="analytical-flat-unit-result-policy",
    policy_version="settlement-policy-v1",
)


@dataclass(frozen=True, slots=True, order=True)
class SettlementEvidence:
    opportunity_id: str
    canonical_event_id: str
    result_snapshot_id: str
    result_checksum_sha256: str
    canonical_result_id: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "opportunity_id": self.opportunity_id,
            "canonical_event_id": self.canonical_event_id,
            "result_snapshot_id": self.result_snapshot_id,
            "result_checksum_sha256": self.result_checksum_sha256,
            "canonical_result_id": self.canonical_result_id,
        }


@dataclass(frozen=True, slots=True)
class AnalyticalSettlement:
    """Immutable simulated flat-unit settlement; never a sportsbook transaction."""

    settlement_id: str
    settlement_version: str
    source_artifact_id: str
    source_artifact_checksum_sha256: str
    position_type: str
    position_id: str
    opportunity_ids: tuple[str, ...]
    canonical_event_ids: tuple[str, ...]
    evidence: tuple[SettlementEvidence, ...]
    settlement_as_of_utc: datetime
    decimal_odds: Decimal
    status: SettlementStatus
    stake_units: Decimal
    returned_units: Decimal
    profit_units: Decimal
    policy_id: str
    policy_version: str
    provenance: str
    warnings: tuple[str, ...] = ()
    leg_statuses: tuple[tuple[str, SettlementStatus], ...] = ()

    def __post_init__(self) -> None:
        if self.settlement_version != SETTLEMENT_VERSION:
            raise SettlementError("unsupported settlement version")
        if self.position_type not in {"single", "combination"}:
            raise SettlementError("position_type must be single or combination")
        if not self.position_id or not self.opportunity_ids:
            raise SettlementError("settlement requires a persisted analytical position")
        if len(self.opportunity_ids) != len(set(self.opportunity_ids)):
            raise SettlementError("settlement opportunity ids must be unique")
        try:
            as_of = require_utc(self.settlement_as_of_utc, field_name="settlement_as_of_utc")
            validate_sha256_checksum(self.source_artifact_checksum_sha256)
        except Exception as exc:
            raise SettlementError(f"invalid settlement identity field: {exc}") from exc
        object.__setattr__(self, "settlement_as_of_utc", as_of)
        for name, value in (
            ("decimal_odds", self.decimal_odds),
            ("stake_units", self.stake_units),
            ("returned_units", self.returned_units),
            ("profit_units", self.profit_units),
        ):
            if not value.is_finite():
                raise SettlementError(f"{name} must be finite")
        if self.decimal_odds <= 1 or self.stake_units != Decimal("1"):
            raise SettlementError("settlement requires odds >1 and a one-unit stake")
        if self.returned_units - self.stake_units != self.profit_units:
            raise SettlementError("returned, stake, and profit units are inconsistent")
        expected = derive_settlement_id(self)
        if self.settlement_id != expected:
            raise SettlementError("settlement id does not match material evidence")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "settlement_id": self.settlement_id,
            "settlement_version": self.settlement_version,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_checksum_sha256": self.source_artifact_checksum_sha256,
            "position_type": self.position_type,
            "position_id": self.position_id,
            "opportunity_ids": list(self.opportunity_ids),
            "canonical_event_ids": list(self.canonical_event_ids),
            "evidence": [item.to_json() for item in self.evidence],
            "settlement_as_of_utc": format_utc_timestamp(self.settlement_as_of_utc),
            "decimal_odds": format(self.decimal_odds, "f"),
            "status": self.status.value,
            "stake_units": format(self.stake_units, "f"),
            "returned_units": format(self.returned_units, "f"),
            "profit_units": format(self.profit_units, "f"),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "provenance": self.provenance,
            "warnings": list(self.warnings),
            "leg_statuses": [
                {"opportunity_id": opportunity_id, "status": status.value}
                for opportunity_id, status in self.leg_statuses
            ],
        }


def settle_single(
    *,
    source_artifact_id: str,
    source_artifact_checksum_sha256: str,
    opportunity_id: str,
    canonical_event_id: str,
    selection: CanonicalSelectionIdentity,
    decimal_odds: Decimal,
    result_snapshot: VerifiedResultSnapshot | None,
    as_of_utc: datetime,
    policy: SettlementPolicy = SETTLEMENT_POLICY_V1,
    provenance: str = "verified-analysis-artifact-and-result-snapshot",
) -> AnalyticalSettlement:
    """Settle one persisted selection against exact canonical result identity."""
    as_of = _utc(as_of_utc)
    evidence: tuple[SettlementEvidence, ...] = ()
    warnings: tuple[str, ...] = ()
    if result_snapshot is None:
        status = SettlementStatus.PENDING
        warnings = ("missing-result-evidence",)
    else:
        result = result_snapshot.result
        if result.canonical_event_id != canonical_event_id:
            raise SettlementError("result canonical event does not match analytical position")
        if result.source_observed_at_utc > as_of:
            raise SettlementError("result evidence was not observable at settlement as-of time")
        evidence = (_evidence(opportunity_id, result_snapshot),)
        status = _single_status(result, selection)
    returned = _single_return(decimal_odds, status, policy)
    return _build(
        source_artifact_id=source_artifact_id,
        source_artifact_checksum_sha256=source_artifact_checksum_sha256,
        position_type="single",
        position_id=opportunity_id,
        opportunity_ids=(opportunity_id,),
        canonical_event_ids=(canonical_event_id,),
        evidence=evidence,
        as_of_utc=as_of,
        decimal_odds=decimal_odds,
        status=status,
        returned_units=returned,
        policy=policy,
        provenance=provenance,
        warnings=warnings,
        leg_statuses=((opportunity_id, status),),
    )


def settle_combination(
    *,
    source_artifact_id: str,
    source_artifact_checksum_sha256: str,
    combination_id: str,
    legs: tuple[
        tuple[
            str,
            str,
            CanonicalSelectionIdentity,
            Decimal,
            VerifiedResultSnapshot | None,
        ],
        ...,
    ],
    persisted_decimal_odds: Decimal,
    as_of_utc: datetime,
    policy: SettlementPolicy = SETTLEMENT_POLICY_V1,
    provenance: str = "verified-analysis-artifact-and-result-snapshots",
) -> AnalyticalSettlement:
    """Settle exact persisted legs without dependency re-evaluation or correlation adjustment."""
    if len(legs) < 2:
        raise SettlementError("combination settlement requires at least two persisted legs")
    if len({item[0] for item in legs}) != len(legs):
        raise SettlementError("combination contains duplicate opportunity ids")
    persisted_product = Decimal("1")
    for _, _, _, odds, _ in legs:
        if not odds.is_finite() or odds <= 1:
            raise SettlementError("combination leg odds must be finite and greater than one")
        persisted_product *= odds
    if (
        not persisted_decimal_odds.is_finite()
        or persisted_decimal_odds <= 1
        or persisted_decimal_odds != persisted_product
    ):
        raise SettlementError("persisted combination odds do not match exact persisted legs")
    as_of = _utc(as_of_utc)
    statuses: list[tuple[str, SettlementStatus]] = []
    evidence: list[SettlementEvidence] = []
    active_odds = Decimal("1")
    for opportunity_id, event_id, selection, odds, snapshot in legs:
        if snapshot is None:
            leg_status = SettlementStatus.PENDING
        else:
            result = snapshot.result
            if result.canonical_event_id != event_id:
                raise SettlementError("combination result references the wrong canonical event")
            if result.source_observed_at_utc > as_of:
                raise SettlementError("combination result was not observable at as-of time")
            leg_status = _single_status(result, selection)
            evidence.append(_evidence(opportunity_id, snapshot))
        statuses.append((opportunity_id, leg_status))
        if leg_status is SettlementStatus.WIN:
            active_odds *= odds
    values = {status for _, status in statuses}
    if SettlementStatus.LOSS in values:
        status = SettlementStatus.LOSS
        returned = Decimal("0")
    elif SettlementStatus.UNRESOLVED in values:
        status = SettlementStatus.UNRESOLVED
        returned = Decimal("1")
    elif SettlementStatus.PENDING in values:
        status = SettlementStatus.PENDING
        returned = Decimal("1")
    elif values <= {SettlementStatus.VOID, SettlementStatus.PUSH}:
        status = SettlementStatus.VOID if SettlementStatus.VOID in values else SettlementStatus.PUSH
        returned = Decimal("1")
    else:
        status = SettlementStatus.WIN
        returned = active_odds
    warnings = (
        ("void-or-push-legs-retained-at-unit-return",)
        if values & {SettlementStatus.VOID, SettlementStatus.PUSH}
        else ()
    )
    return _build(
        source_artifact_id=source_artifact_id,
        source_artifact_checksum_sha256=source_artifact_checksum_sha256,
        position_type="combination",
        position_id=combination_id,
        opportunity_ids=tuple(item[0] for item in legs),
        canonical_event_ids=tuple(item[1] for item in legs),
        evidence=tuple(sorted(evidence)),
        as_of_utc=as_of,
        decimal_odds=persisted_decimal_odds,
        status=status,
        returned_units=returned,
        policy=policy,
        provenance=provenance,
        warnings=warnings,
        leg_statuses=tuple(statuses),
    )


def derive_settlement_id(settlement: AnalyticalSettlement) -> str:
    return content_addressed_id(
        identity_type=SETTLEMENT_VERSION,
        payload=_identity_payload(settlement),
    )


def _build(
    *,
    source_artifact_id: str,
    source_artifact_checksum_sha256: str,
    position_type: str,
    position_id: str,
    opportunity_ids: tuple[str, ...],
    canonical_event_ids: tuple[str, ...],
    evidence: tuple[SettlementEvidence, ...],
    as_of_utc: datetime,
    decimal_odds: Decimal,
    status: SettlementStatus,
    returned_units: Decimal,
    policy: SettlementPolicy,
    provenance: str,
    warnings: tuple[str, ...],
    leg_statuses: tuple[tuple[str, SettlementStatus], ...],
) -> AnalyticalSettlement:
    values = {
        "source_artifact_id": source_artifact_id,
        "source_artifact_checksum_sha256": source_artifact_checksum_sha256,
        "position_type": position_type,
        "position_id": position_id,
        "opportunity_ids": opportunity_ids,
        "canonical_event_ids": canonical_event_ids,
        "evidence": evidence,
        "settlement_as_of_utc": as_of_utc,
        "decimal_odds": decimal_odds,
        "status": status,
        "stake_units": policy.stake_units,
        "returned_units": returned_units,
        "profit_units": returned_units - policy.stake_units,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "provenance": provenance,
        "warnings": tuple(sorted(warnings)),
        "leg_statuses": leg_statuses,
    }
    provisional = AnalyticalSettlement.__new__(AnalyticalSettlement)
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    identity = content_addressed_id(
        identity_type=SETTLEMENT_VERSION,
        payload=_identity_payload(provisional),
    )
    return AnalyticalSettlement(
        settlement_id=identity,
        settlement_version=SETTLEMENT_VERSION,
        **values,  # type: ignore[arg-type]
    )


def _identity_payload(settlement: AnalyticalSettlement) -> dict[str, JsonValue]:
    return {
        "source_artifact_id": settlement.source_artifact_id,
        "source_artifact_checksum_sha256": settlement.source_artifact_checksum_sha256,
        "position_type": settlement.position_type,
        "position_id": settlement.position_id,
        "opportunity_ids": list(settlement.opportunity_ids),
        "canonical_event_ids": list(settlement.canonical_event_ids),
        "evidence": [item.to_json() for item in settlement.evidence],
        "settlement_as_of_utc": format_utc_timestamp(settlement.settlement_as_of_utc),
        "decimal_odds": format(settlement.decimal_odds, "f"),
        "status": settlement.status.value,
        "stake_units": format(settlement.stake_units, "f"),
        "returned_units": format(settlement.returned_units, "f"),
        "profit_units": format(settlement.profit_units, "f"),
        "policy_id": settlement.policy_id,
        "policy_version": settlement.policy_version,
        "provenance": settlement.provenance,
        "warnings": list(settlement.warnings),
        "leg_statuses": [
            {"opportunity_id": opportunity_id, "status": status.value}
            for opportunity_id, status in settlement.leg_statuses
        ],
    }


def _single_status(
    result: CanonicalResult,
    selection: CanonicalSelectionIdentity,
) -> SettlementStatus:
    status = result.event_status
    if status in {EventResultStatus.SCHEDULED, EventResultStatus.IN_PROGRESS}:
        return SettlementStatus.PENDING
    if status in {EventResultStatus.POSTPONED}:
        return SettlementStatus.PENDING
    if status in {EventResultStatus.CANCELLED, EventResultStatus.ABANDONED}:
        return SettlementStatus.VOID
    if status is EventResultStatus.INCOMPLETE:
        return SettlementStatus.UNRESOLVED
    try:
        outcome = result.outcome_for(selection)
    except ResultError as exc:
        raise SettlementError(
            "selection/result identity mismatch; missing outcomes are never inferred"
        ) from exc
    return SettlementStatus(outcome)


def _single_return(
    decimal_odds: Decimal,
    status: SettlementStatus,
    policy: SettlementPolicy,
) -> Decimal:
    if not decimal_odds.is_finite() or decimal_odds <= 1:
        raise SettlementError("decimal odds must be finite and greater than one")
    if status is SettlementStatus.WIN:
        return decimal_odds
    if status is SettlementStatus.LOSS:
        return Decimal("0")
    if status in {
        SettlementStatus.PUSH,
        SettlementStatus.VOID,
        SettlementStatus.PENDING,
        SettlementStatus.UNRESOLVED,
    }:
        return policy.stake_units
    raise SettlementError("unsupported settlement status")


def _evidence(
    opportunity_id: str,
    snapshot: VerifiedResultSnapshot,
) -> SettlementEvidence:
    return SettlementEvidence(
        opportunity_id=opportunity_id,
        canonical_event_id=snapshot.result.canonical_event_id,
        result_snapshot_id=snapshot.snapshot_id,
        result_checksum_sha256=snapshot.checksum_sha256,
        canonical_result_id=snapshot.result.canonical_result_id,
    )


def _utc(value: datetime) -> datetime:
    try:
        return require_utc(value, field_name="as_of_utc")
    except Exception as exc:
        raise SettlementError(str(exc)) from exc

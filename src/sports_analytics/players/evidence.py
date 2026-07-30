"""Strict sport-aware player, lineup, injury, and availability evidence."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from sports_analytics.artifact_strict import (
    require_canonical_utc_timestamp_string,
    require_dict,
    require_list,
    require_str,
)
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, FeatureError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.sports.contracts import require_utc

PLAYER_EVIDENCE_TYPE: Final[str] = "canonical-player-evidence"
PLAYER_EVIDENCE_SCHEMA: Final[str] = "canonical-player-evidence-v1"
PLAYER_IMPORT_SCHEMA: Final[str] = "operator-player-evidence-import-v1"


class PlayerRole(StrEnum):
    GOALKEEPER = "goalkeeper"
    DEFENDER = "defender"
    MIDFIELDER = "midfielder"
    ATTACKER = "attacker"
    UNKNOWN = "unknown"


class PlayerEvidenceState(StrEnum):
    AVAILABLE = "available"
    DOUBTFUL = "doubtful"
    INJURED = "injured"
    SUSPENDED = "suspended"
    UNAVAILABLE_OTHER = "unavailable-other"
    EXPECTED_STARTER = "expected-starter"
    EXPECTED_BENCH = "expected-bench"
    CONFIRMED_STARTER = "confirmed-starter"
    CONFIRMED_BENCH = "confirmed-bench"
    NOT_IN_SQUAD = "not-in-squad"
    UNKNOWN = "unknown"


class PlayerEvidenceType(StrEnum):
    MEDICAL_INJURY = "medical-injury"
    SPORTING_SUSPENSION = "sporting-suspension"
    SELECTION_DECISION = "selection-decision"
    EXPECTED_LINEUP = "expected-lineup"
    CONFIRMED_LINEUP = "confirmed-lineup"
    EXPECTED_MINUTES = "expected-minutes"
    PARTICIPATION_RESULT = "participation-result"
    MATCH_STATISTICS = "match-statistics"


class PlayerReconciliationState(StrEnum):
    EXACT = "exact"
    MANUAL = "manual"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True, order=True)
class Player:
    canonical_player_id: str
    sport_code: str

    def __post_init__(self) -> None:
        _text(self.canonical_player_id, "canonical_player_id")
        _text(self.sport_code, "sport_code")


@dataclass(frozen=True, slots=True, order=True)
class SourcePlayer:
    source_name: str
    source_player_id: str
    sport_code: str
    display_name: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_name, "source_name"),
            (self.source_player_id, "source_player_id"),
            (self.sport_code, "sport_code"),
            (self.display_name, "display_name"),
        ):
            _text(value, field)


@dataclass(frozen=True, slots=True, order=True)
class PlayerIdentityReconciliation:
    source_name: str
    source_player_id: str
    canonical_player_id: str | None
    state: PlayerReconciliationState
    confidence: float
    policy_version: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.source_name, "source_name")
        _text(self.source_player_id, "source_player_id")
        _text(self.policy_version, "policy_version")
        _probability(self.confidence, "confidence")
        if self.state is PlayerReconciliationState.UNRESOLVED:
            if self.canonical_player_id is not None or not self.reason:
                raise FeatureError(
                    "unresolved player identity requires a reason and no canonical id"
                )
        elif self.canonical_player_id is None:
            raise FeatureError("resolved player identity requires a canonical id")
        elif self.confidence != 1.0:
            raise FeatureError("exact/manual player reconciliation requires full confidence")


@dataclass(frozen=True, slots=True, order=True)
class PlayerTeamMembership:
    canonical_player_id: str
    canonical_team_id: str
    sport_code: str
    valid_from: date
    valid_to: date | None
    role: PlayerRole

    def __post_init__(self) -> None:
        for value, field in (
            (self.canonical_player_id, "canonical_player_id"),
            (self.canonical_team_id, "canonical_team_id"),
            (self.sport_code, "sport_code"),
        ):
            _text(value, field)
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise FeatureError("player membership date bounds are reversed")

    def contains(self, value: date) -> bool:
        return value >= self.valid_from and (self.valid_to is None or value <= self.valid_to)


@dataclass(frozen=True, slots=True, order=True)
class PlayerAvailabilityObservation:
    canonical_player_id: str | None
    source_player_id: str
    canonical_team_id: str
    sport_code: str
    source_name: str
    source_observation_id: str
    observed_at_utc: datetime
    event_id: str | None
    effective_date: date
    event_start_utc: datetime | None
    status: PlayerEvidenceState
    confidence: float
    evidence_type: PlayerEvidenceType
    valid_until_utc: datetime | None = None
    superseded_by_observation_id: str | None = None
    expected_minutes: int | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_player_id, "source_player_id"),
            (self.canonical_team_id, "canonical_team_id"),
            (self.sport_code, "sport_code"),
            (self.source_name, "source_name"),
            (self.source_observation_id, "source_observation_id"),
        ):
            _text(value, field)
        if self.canonical_player_id is not None:
            _text(self.canonical_player_id, "canonical_player_id")
        observed = require_utc(self.observed_at_utc, field_name="observed_at_utc")
        if self.event_start_utc is not None:
            start = require_utc(self.event_start_utc, field_name="event_start_utc")
            if (
                self.evidence_type
                not in {
                    PlayerEvidenceType.PARTICIPATION_RESULT,
                    PlayerEvidenceType.MATCH_STATISTICS,
                }
                and observed >= start
            ):
                raise FeatureError("pre-match player observation must precede event kickoff")
        if self.valid_until_utc is not None:
            valid_until = require_utc(self.valid_until_utc, field_name="valid_until_utc")
            if valid_until < observed:
                raise FeatureError("player evidence valid-until precedes observation")
        _probability(self.confidence, "confidence")
        if self.expected_minutes is not None and (
            type(self.expected_minutes) is not int or not 0 <= self.expected_minutes <= 120
        ):
            raise FeatureError("expected minutes must be an integer in [0, 120]")
        if self.evidence_type is PlayerEvidenceType.EXPECTED_MINUTES:
            if self.expected_minutes is None:
                raise FeatureError("expected-minutes evidence requires expected minutes")
        elif self.expected_minutes is not None:
            raise FeatureError("expected minutes belong only to expected-minutes evidence")
        _validate_state_semantics(self.status, self.evidence_type)

    @property
    def observation_id(self) -> str:
        return content_addressed_id(
            identity_type="canonical-player-observation-v1",
            payload=self.to_json(),
        )

    def is_stale(self, as_of_utc: datetime) -> bool:
        as_of = require_utc(as_of_utc, field_name="as_of_utc")
        return self.valid_until_utc is not None and as_of > self.valid_until_utc

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "source_player_id": self.source_player_id,
            "canonical_team_id": self.canonical_team_id,
            "sport_code": self.sport_code,
            "source_name": self.source_name,
            "source_observation_id": self.source_observation_id,
            "observed_at_utc": format_utc_timestamp(self.observed_at_utc),
            "event_id": self.event_id,
            "effective_date": self.effective_date.isoformat(),
            "event_start_utc": (
                None if self.event_start_utc is None else format_utc_timestamp(self.event_start_utc)
            ),
            "status": self.status.value,
            "confidence": self.confidence,
            "evidence_type": self.evidence_type.value,
            "valid_until_utc": (
                None if self.valid_until_utc is None else format_utc_timestamp(self.valid_until_utc)
            ),
            "superseded_by_observation_id": self.superseded_by_observation_id,
            "expected_minutes": self.expected_minutes,
        }


# Explicit semantic names used by downstream boundaries.
InjuryStatus = PlayerAvailabilityObservation
SuspensionStatus = PlayerAvailabilityObservation
ExpectedLineupObservation = PlayerAvailabilityObservation
ConfirmedLineupObservation = PlayerAvailabilityObservation
ExpectedMinutesObservation = PlayerAvailabilityObservation
PlayerParticipationResult = PlayerAvailabilityObservation
PlayerMatchStatistics = PlayerAvailabilityObservation


@dataclass(frozen=True, slots=True)
class PlayerEvidenceBundle:
    players: tuple[Player, ...]
    source_players: tuple[SourcePlayer, ...]
    reconciliations: tuple[PlayerIdentityReconciliation, ...]
    memberships: tuple[PlayerTeamMembership, ...]
    observations: tuple[PlayerAvailabilityObservation, ...]
    historical_equivalence_state: str = "player-context-not-trainable"

    def __post_init__(self) -> None:
        if self.historical_equivalence_state not in {
            "player-context-not-trainable",
            "historical-equivalence-verified",
        }:
            raise FeatureError("player historical equivalence state is invalid")
        source_keys = [(item.source_name, item.source_player_id) for item in self.source_players]
        if len(source_keys) != len(set(source_keys)):
            raise FeatureError("duplicate source player identity")
        observation_keys = [
            (item.source_name, item.source_observation_id) for item in self.observations
        ]
        if len(observation_keys) != len(set(observation_keys)):
            raise FeatureError("duplicate player observation identity")
        confirmed: dict[tuple[str, str], PlayerEvidenceState] = {}
        memberships = {
            (item.canonical_player_id, item.canonical_team_id): item for item in self.memberships
        }
        for item in self.observations:
            if item.canonical_player_id is not None:
                membership = memberships.get((item.canonical_player_id, item.canonical_team_id))
                if membership is None or not membership.contains(item.effective_date):
                    raise FeatureError("player observation falls outside verified membership")
            if item.evidence_type is PlayerEvidenceType.CONFIRMED_LINEUP:
                if item.event_id is None or item.canonical_player_id is None:
                    raise FeatureError("confirmed lineup requires event and canonical player")
                key = (item.event_id, item.canonical_player_id)
                previous = confirmed.get(key)
                if previous is not None and previous != item.status:
                    raise FeatureError("contradictory confirmed lineup observations")
                confirmed[key] = item.status

    @property
    def model_use_state(self) -> str:
        return (
            "player-context-consumable"
            if self.historical_equivalence_state == "historical-equivalence-verified"
            else "display-only-current-context"
        )


def publish_player_evidence_artifact(
    *,
    root: Path,
    relative_directory: str,
    bundle: PlayerEvidenceBundle,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=PLAYER_EVIDENCE_TYPE,
        schema_version=PLAYER_EVIDENCE_SCHEMA,
        payload=_bundle_payload(bundle),
    )


def load_player_evidence_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> tuple[AnalyticalArtifact, PlayerEvidenceBundle]:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=PLAYER_EVIDENCE_TYPE,
        expected_schema_version=PLAYER_EVIDENCE_SCHEMA,
        expected_checksum=expected_checksum,
    )
    return artifact, _parse_bundle(artifact.payload)


def player_csv_template() -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(_IMPORT_FIELDS), lineterminator="\n")
    writer.writeheader()
    writer.writerow(_template_row())
    return output.getvalue()


def player_json_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": PLAYER_IMPORT_SCHEMA,
                "observations": [_template_row()],
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def parse_player_import_bundle_csv(text: str) -> PlayerEvidenceBundle:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(_IMPORT_FIELDS):
        raise FeatureError("player CSV columns are not exact")
    return _parse_import_bundle(tuple(dict(row) for row in reader))


def parse_player_import_bundle_json(text: str) -> PlayerEvidenceBundle:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeatureError("player JSON is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "observations"}:
        raise FeatureError("player JSON fields are not exact")
    if payload["schema_version"] != PLAYER_IMPORT_SCHEMA or not isinstance(
        payload["observations"], list
    ):
        raise FeatureError("player JSON schema is invalid")
    rows: list[dict[str, object]] = []
    for raw in payload["observations"]:
        if not isinstance(raw, dict) or set(raw) != set(_IMPORT_FIELDS):
            raise FeatureError("player JSON observation fields are not exact")
        rows.append(raw)
    return _parse_import_bundle(tuple(rows))


def parse_player_import_csv(text: str) -> tuple[PlayerAvailabilityObservation, ...]:
    """Parse the exact CSV boundary and return its validated observations."""
    return parse_player_import_bundle_csv(text).observations


def parse_player_import_json(text: str) -> tuple[PlayerAvailabilityObservation, ...]:
    """Parse the exact JSON boundary and return its validated observations."""
    return parse_player_import_bundle_json(text).observations


def player_capability_matrix() -> tuple[tuple[str, str], ...]:
    return (
        ("current-player-context-display", "operator-evidence-supported"),
        ("team-level-player-features", "player-context-not-trainable"),
        ("player-availability-adjustment", "player-context-not-trainable"),
        ("anytime-scorer", "player-data-required"),
        ("first-scorer", "player-data-required"),
        ("player-shots", "player-data-required"),
        ("player-shots-on-target", "player-data-required"),
        ("player-goals", "player-data-required"),
        ("player-assists", "player-data-required"),
        ("player-cards", "player-data-required"),
        ("other-player-props", "player-data-required"),
    )


_OBSERVATION_FIELDS: Final[tuple[str, ...]] = (
    "canonical_player_id",
    "source_player_id",
    "canonical_team_id",
    "sport_code",
    "source_name",
    "source_observation_id",
    "observed_at_utc",
    "event_id",
    "effective_date",
    "event_start_utc",
    "status",
    "confidence",
    "evidence_type",
    "valid_until_utc",
    "superseded_by_observation_id",
    "expected_minutes",
)
_IMPORT_FIELDS: Final[tuple[str, ...]] = (
    "canonical_player_id",
    "source_player_id",
    "source_player_display_name",
    "reconciliation_state",
    "reconciliation_confidence",
    "reconciliation_reason",
    "canonical_team_id",
    "sport_code",
    "membership_valid_from",
    "membership_valid_to",
    "player_role",
    "source_name",
    "source_observation_id",
    "observed_at_utc",
    "event_id",
    "effective_date",
    "event_start_utc",
    "status",
    "confidence",
    "evidence_type",
    "valid_until_utc",
    "superseded_by_observation_id",
    "expected_minutes",
)


def _template_row() -> dict[str, str]:
    return {
        "canonical_player_id": "",
        "source_player_id": "source-player-001",
        "source_player_display_name": "Unresolved player",
        "reconciliation_state": "unresolved",
        "reconciliation_confidence": "0.0",
        "reconciliation_reason": "identity-not-established",
        "canonical_team_id": "canonical-team-001",
        "sport_code": "football",
        "membership_valid_from": "",
        "membership_valid_to": "",
        "player_role": "unknown",
        "source_name": "operator-reviewed",
        "source_observation_id": "observation-001",
        "observed_at_utc": "2026-01-01T10:00:00Z",
        "event_id": "canonical-event-001",
        "effective_date": "2026-01-01",
        "event_start_utc": "2026-01-01T15:00:00Z",
        "status": "unknown",
        "confidence": "0.0",
        "evidence_type": "selection-decision",
        "valid_until_utc": "2026-01-01T15:00:00Z",
        "superseded_by_observation_id": "",
        "expected_minutes": "",
    }


def _parse_import_bundle(rows: tuple[dict[str, object], ...]) -> PlayerEvidenceBundle:
    if not rows:
        raise FeatureError("player import requires at least one observation")
    observations: list[PlayerAvailabilityObservation] = []
    players: dict[str, Player] = {}
    source_players: dict[tuple[str, str], SourcePlayer] = {}
    reconciliations: dict[tuple[str, str], PlayerIdentityReconciliation] = {}
    memberships: dict[tuple[str, str, date, date | None], PlayerTeamMembership] = {}
    for raw in rows:
        observation = _parse_import_row(raw)
        observations.append(observation)
        source_key = (observation.source_name, observation.source_player_id)
        source_player = SourcePlayer(
            source_name=observation.source_name,
            source_player_id=observation.source_player_id,
            sport_code=observation.sport_code,
            display_name=_required_text(
                raw["source_player_display_name"],
                "source_player_display_name",
            ),
        )
        _same_or_add(source_players, source_key, source_player, "source player")
        try:
            reconciliation_state = PlayerReconciliationState(
                _required_text(raw["reconciliation_state"], "reconciliation_state")
            )
            reconciliation_confidence_raw = raw["reconciliation_confidence"]
            if isinstance(reconciliation_confidence_raw, bool) or not isinstance(
                reconciliation_confidence_raw,
                int | float | str,
            ):
                raise ValueError
            reconciliation_confidence = float(reconciliation_confidence_raw)
        except (TypeError, ValueError) as exc:
            raise FeatureError("player reconciliation contains invalid typed values") from exc
        reconciliation = PlayerIdentityReconciliation(
            source_name=observation.source_name,
            source_player_id=observation.source_player_id,
            canonical_player_id=observation.canonical_player_id,
            state=reconciliation_state,
            confidence=reconciliation_confidence,
            policy_version="operator-player-reconciliation-v1",
            reason=_optional_text(raw["reconciliation_reason"], "reconciliation_reason"),
        )
        _same_or_add(
            reconciliations,
            source_key,
            reconciliation,
            "player reconciliation",
        )
        canonical_player_id = observation.canonical_player_id
        if canonical_player_id is None:
            if any(
                raw[field] not in {"", None}
                for field in ("membership_valid_from", "membership_valid_to")
            ):
                raise FeatureError("unresolved player import cannot declare membership")
            continue
        player = Player(canonical_player_id, observation.sport_code)
        _same_or_add(players, canonical_player_id, player, "canonical player")
        valid_from_text = _required_text(
            raw["membership_valid_from"],
            "membership_valid_from",
        )
        valid_to_text = _optional_text(raw["membership_valid_to"], "membership_valid_to")
        try:
            valid_from = date.fromisoformat(valid_from_text)
            valid_to = None if valid_to_text is None else date.fromisoformat(valid_to_text)
            role = PlayerRole(_required_text(raw["player_role"], "player_role"))
        except ValueError as exc:
            raise FeatureError("player membership contains invalid typed values") from exc
        membership = PlayerTeamMembership(
            canonical_player_id=canonical_player_id,
            canonical_team_id=observation.canonical_team_id,
            sport_code=observation.sport_code,
            valid_from=valid_from,
            valid_to=valid_to,
            role=role,
        )
        membership_key = (
            canonical_player_id,
            observation.canonical_team_id,
            valid_from,
            valid_to,
        )
        _same_or_add(memberships, membership_key, membership, "player membership")
    return PlayerEvidenceBundle(
        players=tuple(sorted(players.values())),
        source_players=tuple(sorted(source_players.values())),
        reconciliations=tuple(sorted(reconciliations.values())),
        memberships=tuple(sorted(memberships.values())),
        observations=tuple(sorted(observations)),
    )


def _same_or_add[K, V](
    values: dict[K, V],
    key: K,
    value: V,
    description: str,
) -> None:
    previous = values.get(key)
    if previous is not None and previous != value:
        raise FeatureError(f"conflicting {description} declarations")
    values[key] = value


def _parse_import_row(raw: dict[str, object]) -> PlayerAvailabilityObservation:
    canonical = _optional_text(raw["canonical_player_id"], "canonical_player_id")
    expected_minutes_raw = raw["expected_minutes"]
    expected_minutes = (
        None
        if expected_minutes_raw in {"", None}
        else _integer(expected_minutes_raw, "expected_minutes")
    )
    try:
        effective = date.fromisoformat(_required_text(raw["effective_date"], "effective_date"))
        confidence_raw = raw["confidence"]
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, int | float | str):
            raise ValueError
        confidence = float(confidence_raw)
        status = PlayerEvidenceState(_required_text(raw["status"], "status"))
        evidence_type = PlayerEvidenceType(_required_text(raw["evidence_type"], "evidence_type"))
    except (ValueError, TypeError) as exc:
        raise FeatureError("player import contains invalid typed values") from exc
    return PlayerAvailabilityObservation(
        canonical_player_id=canonical,
        source_player_id=_required_text(raw["source_player_id"], "source_player_id"),
        canonical_team_id=_required_text(raw["canonical_team_id"], "canonical_team_id"),
        sport_code=_required_text(raw["sport_code"], "sport_code"),
        source_name=_required_text(raw["source_name"], "source_name"),
        source_observation_id=_required_text(
            raw["source_observation_id"],
            "source_observation_id",
        ),
        observed_at_utc=_import_timestamp(raw["observed_at_utc"], "observed_at_utc"),
        event_id=_optional_text(raw["event_id"], "event_id"),
        effective_date=effective,
        event_start_utc=_optional_timestamp(raw["event_start_utc"], "event_start_utc"),
        status=status,
        confidence=confidence,
        evidence_type=evidence_type,
        valid_until_utc=_optional_timestamp(raw["valid_until_utc"], "valid_until_utc"),
        superseded_by_observation_id=_optional_text(
            raw["superseded_by_observation_id"],
            "superseded_by_observation_id",
        ),
        expected_minutes=expected_minutes,
    )


def _bundle_payload(bundle: PlayerEvidenceBundle) -> dict[str, JsonValue]:
    return {
        "players": [
            {"canonical_player_id": item.canonical_player_id, "sport_code": item.sport_code}
            for item in sorted(bundle.players)
        ],
        "source_players": [
            {
                "source_name": item.source_name,
                "source_player_id": item.source_player_id,
                "sport_code": item.sport_code,
                "display_name": item.display_name,
            }
            for item in sorted(bundle.source_players)
        ],
        "reconciliations": [
            {
                "source_name": item.source_name,
                "source_player_id": item.source_player_id,
                "canonical_player_id": item.canonical_player_id,
                "state": item.state.value,
                "confidence": item.confidence,
                "policy_version": item.policy_version,
                "reason": item.reason,
            }
            for item in sorted(bundle.reconciliations)
        ],
        "memberships": [
            {
                "canonical_player_id": item.canonical_player_id,
                "canonical_team_id": item.canonical_team_id,
                "sport_code": item.sport_code,
                "valid_from": item.valid_from.isoformat(),
                "valid_to": None if item.valid_to is None else item.valid_to.isoformat(),
                "role": item.role.value,
            }
            for item in sorted(bundle.memberships)
        ],
        "observations": [item.to_json() for item in sorted(bundle.observations)],
        "historical_equivalence_state": bundle.historical_equivalence_state,
        "model_use_state": bundle.model_use_state,
    }


def _parse_bundle(payload: object) -> PlayerEvidenceBundle:
    row = require_dict(payload, field="player_evidence")
    if set(row) != {
        "players",
        "source_players",
        "reconciliations",
        "memberships",
        "observations",
        "historical_equivalence_state",
        "model_use_state",
    }:
        raise ArtifactError("player evidence payload fields are not exact")
    players = tuple(
        Player(
            require_str(item["canonical_player_id"], field="canonical_player_id"),
            require_str(item["sport_code"], field="sport_code"),
        )
        for raw in require_list(row["players"], field="players")
        for item in (require_dict(raw, field="players[]"),)
        if _exact(item, {"canonical_player_id", "sport_code"}, "player")
    )
    source_players = tuple(
        SourcePlayer(
            require_str(item["source_name"], field="source_name"),
            require_str(item["source_player_id"], field="source_player_id"),
            require_str(item["sport_code"], field="sport_code"),
            require_str(item["display_name"], field="display_name"),
        )
        for raw in require_list(row["source_players"], field="source_players")
        for item in (require_dict(raw, field="source_players[]"),)
        if _exact(
            item,
            {"source_name", "source_player_id", "sport_code", "display_name"},
            "source player",
        )
    )
    reconciliations = tuple(
        _parse_reconciliation(require_dict(raw, field="reconciliations[]"))
        for raw in require_list(row["reconciliations"], field="reconciliations")
    )
    memberships = tuple(
        _parse_membership(require_dict(raw, field="memberships[]"))
        for raw in require_list(row["memberships"], field="memberships")
    )
    observations = tuple(
        _parse_persisted_observation(require_dict(raw, field="observations[]"))
        for raw in require_list(row["observations"], field="observations")
    )
    bundle = PlayerEvidenceBundle(
        players=players,
        source_players=source_players,
        reconciliations=reconciliations,
        memberships=memberships,
        observations=observations,
        historical_equivalence_state=require_str(
            row["historical_equivalence_state"],
            field="historical_equivalence_state",
        ),
    )
    if row["model_use_state"] != bundle.model_use_state:
        raise ArtifactError("player model-use state is forged or stale")
    return bundle


def _parse_reconciliation(row: dict[str, JsonValue]) -> PlayerIdentityReconciliation:
    _exact(
        row,
        {
            "source_name",
            "source_player_id",
            "canonical_player_id",
            "state",
            "confidence",
            "policy_version",
            "reason",
        },
        "player reconciliation",
    )
    canonical = row["canonical_player_id"]
    reason = row["reason"]
    return PlayerIdentityReconciliation(
        source_name=require_str(row["source_name"], field="source_name"),
        source_player_id=require_str(row["source_player_id"], field="source_player_id"),
        canonical_player_id=(
            None if canonical is None else require_str(canonical, field="canonical_player_id")
        ),
        state=PlayerReconciliationState(require_str(row["state"], field="state")),
        confidence=_number(row["confidence"], "confidence"),
        policy_version=require_str(row["policy_version"], field="policy_version"),
        reason=None if reason is None else require_str(reason, field="reason"),
    )


def _parse_membership(row: dict[str, JsonValue]) -> PlayerTeamMembership:
    _exact(
        row,
        {
            "canonical_player_id",
            "canonical_team_id",
            "sport_code",
            "valid_from",
            "valid_to",
            "role",
        },
        "player membership",
    )
    valid_to = row["valid_to"]
    return PlayerTeamMembership(
        canonical_player_id=require_str(row["canonical_player_id"], field="canonical_player_id"),
        canonical_team_id=require_str(row["canonical_team_id"], field="canonical_team_id"),
        sport_code=require_str(row["sport_code"], field="sport_code"),
        valid_from=date.fromisoformat(require_str(row["valid_from"], field="valid_from")),
        valid_to=(
            None
            if valid_to is None
            else date.fromisoformat(require_str(valid_to, field="valid_to"))
        ),
        role=PlayerRole(require_str(row["role"], field="role")),
    )


def _parse_persisted_observation(row: dict[str, JsonValue]) -> PlayerAvailabilityObservation:
    if set(row) != set(_OBSERVATION_FIELDS):
        raise ArtifactError("persisted player observation fields are not exact")
    return _parse_import_row(dict(row))


def _validate_state_semantics(
    state: PlayerEvidenceState,
    evidence_type: PlayerEvidenceType,
) -> None:
    allowed = {
        PlayerEvidenceType.MEDICAL_INJURY: {
            PlayerEvidenceState.AVAILABLE,
            PlayerEvidenceState.DOUBTFUL,
            PlayerEvidenceState.INJURED,
            PlayerEvidenceState.UNKNOWN,
        },
        PlayerEvidenceType.SPORTING_SUSPENSION: {
            PlayerEvidenceState.AVAILABLE,
            PlayerEvidenceState.SUSPENDED,
            PlayerEvidenceState.UNKNOWN,
        },
        PlayerEvidenceType.SELECTION_DECISION: {
            PlayerEvidenceState.AVAILABLE,
            PlayerEvidenceState.UNAVAILABLE_OTHER,
            PlayerEvidenceState.NOT_IN_SQUAD,
            PlayerEvidenceState.UNKNOWN,
        },
        PlayerEvidenceType.EXPECTED_LINEUP: {
            PlayerEvidenceState.EXPECTED_STARTER,
            PlayerEvidenceState.EXPECTED_BENCH,
            PlayerEvidenceState.UNKNOWN,
        },
        PlayerEvidenceType.CONFIRMED_LINEUP: {
            PlayerEvidenceState.CONFIRMED_STARTER,
            PlayerEvidenceState.CONFIRMED_BENCH,
            PlayerEvidenceState.NOT_IN_SQUAD,
        },
        PlayerEvidenceType.EXPECTED_MINUTES: {
            PlayerEvidenceState.EXPECTED_STARTER,
            PlayerEvidenceState.EXPECTED_BENCH,
            PlayerEvidenceState.UNKNOWN,
        },
        PlayerEvidenceType.PARTICIPATION_RESULT: {
            PlayerEvidenceState.CONFIRMED_STARTER,
            PlayerEvidenceState.CONFIRMED_BENCH,
            PlayerEvidenceState.NOT_IN_SQUAD,
        },
        PlayerEvidenceType.MATCH_STATISTICS: {
            PlayerEvidenceState.CONFIRMED_STARTER,
            PlayerEvidenceState.CONFIRMED_BENCH,
        },
    }
    if state not in allowed[evidence_type]:
        raise FeatureError("player evidence state contradicts its evidence type")


def _exact(row: dict[str, JsonValue], fields: set[str], label: str) -> bool:
    if set(row) != fields:
        raise ArtifactError(f"{label} fields are not exact")
    return True


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FeatureError(f"{field} must be non-empty canonical text")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value in {"", None}:
        return None
    return _required_text(value, field)


def _text(value: str, field: str) -> str:
    return _required_text(value, field)


def _probability(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise FeatureError(f"{field} must lie in [0, 1]")
    return float(value)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ArtifactError(f"{field} must be numeric")
    return float(value)


def _integer(value: object, field: str) -> int:
    try:
        parsed = int(str(value), 10)
    except ValueError as exc:
        raise FeatureError(f"{field} must be an integer") from exc
    return parsed


def _import_timestamp(value: object, field: str) -> datetime:
    try:
        return require_canonical_utc_timestamp_string(value, field=field)
    except ArtifactError as exc:
        raise FeatureError(str(exc)) from exc


def _optional_timestamp(value: object, field: str) -> datetime | None:
    return None if value in {"", None} else _import_timestamp(value, field)

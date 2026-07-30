"""Verified immutable football participant registration."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final, cast

from sports_analytics.artifact_strict import require_dict, require_list, require_str
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ConfigurationError
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_identifier, validate_sha256_checksum
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sports.contracts import (
    DOWNSTREAM_SAFE_RECONCILIATION_STATES,
    ParticipantType,
    ReconciliationState,
    require_utc,
    validate_display_name,
)

PARTICIPANT_REGISTRY_ARTIFACT_TYPE: Final[str] = "canonical-football-participant-registry"
PARTICIPANT_REGISTRY_SCHEMA: Final[str] = "canonical-football-participant-registry-v1"
PARTICIPANT_REGISTRY_INPUT_SCHEMA: Final[str] = "canonical-football-participant-registry-input-v1"
PARTICIPANT_REGISTRY_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "canonical_participant_id",
        "sport_code",
        "participant_kind",
        "canonical_display_name",
        "competition_ids",
        "reconciliation_state",
        "source_name",
        "source_participant_id",
        "source_lineage_artifact_id",
        "source_lineage_checksum_sha256",
        "valid_from",
        "valid_until",
    }
)
_SAFE_STATES: Final[frozenset[str]] = frozenset(
    state.value for state in DOWNSTREAM_SAFE_RECONCILIATION_STATES
)
_PROHIBITED = re.compile(
    r"(?i)(?:https?://|www\.|authorization|cookie|token|selector|script|"
    r"<[a-z][^>]*>|(?:^|[\\/])[A-Za-z]:[\\/]|(?:^|[\\/])Users[\\/])"
)


@dataclass(frozen=True, slots=True, order=True)
class RegisteredFootballParticipant:
    canonical_participant_id: str
    sport_code: str
    participant_kind: str
    canonical_display_name: str
    competition_ids: tuple[str, ...]
    reconciliation_state: str
    source_name: str
    source_participant_id: str
    source_lineage_artifact_id: str
    source_lineage_checksum_sha256: str
    valid_from: date
    valid_until: date | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "canonical_participant_id": self.canonical_participant_id,
            "sport_code": self.sport_code,
            "participant_kind": self.participant_kind,
            "canonical_display_name": self.canonical_display_name,
            "competition_ids": list(self.competition_ids),
            "reconciliation_state": self.reconciliation_state,
            "source_name": self.source_name,
            "source_participant_id": self.source_participant_id,
            "source_lineage_artifact_id": self.source_lineage_artifact_id,
            "source_lineage_checksum_sha256": self.source_lineage_checksum_sha256,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": None if self.valid_until is None else self.valid_until.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FootballParticipantRegistry:
    artifact: AnalyticalArtifact
    generated_at_utc: datetime
    evaluated_at_utc: datetime
    registry_revision: str
    participants: tuple[RegisteredFootballParticipant, ...]

    def participant(self, canonical_participant_id: str) -> RegisteredFootballParticipant | None:
        return next(
            (
                item
                for item in self.participants
                if item.canonical_participant_id == canonical_participant_id
            ),
            None,
        )

    def require_registered_participant(
        self,
        canonical_participant_id: str,
        *,
        competition_id: str,
        event_date: date,
    ) -> RegisteredFootballParticipant:
        item = self.participant(canonical_participant_id)
        if item is None:
            raise ConfigurationError("football participant is not registered")
        if competition_id not in item.competition_ids:
            raise ConfigurationError("football participant is outside competition scope")
        if event_date < item.valid_from or (
            item.valid_until is not None and event_date > item.valid_until
        ):
            raise ConfigurationError("football participant registration is not valid on event date")
        return item

    def participants_for_competition(
        self, competition_id: str
    ) -> tuple[RegisteredFootballParticipant, ...]:
        return tuple(item for item in self.participants if competition_id in item.competition_ids)


def participant_registry_json_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": PARTICIPANT_REGISTRY_INPUT_SCHEMA,
                "registry_revision": "reviewed-registry-2026-08-01",
                "generated_at_utc": "2026-08-01T12:00:00.000000Z",
                "evaluated_at_utc": "2026-08-01T12:00:00.000000Z",
                "participants": [
                    {
                        "canonical_participant_id": "11111111-1111-5111-8111-111111111111",
                        "sport_code": "football",
                        "participant_kind": "team",
                        "canonical_display_name": "Reviewed Team",
                        "competition_ids": ["prt-primeira-liga"],
                        "reconciliation_state": "exact",
                        "source_name": "verified-football-snapshot",
                        "source_participant_id": "verified-team-1",
                        "source_lineage_artifact_id": "verified-snapshot-artifact",
                        "source_lineage_checksum_sha256": "0" * 64,
                        "valid_from": "2026-07-01",
                        "valid_until": None,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def parse_participant_registry_json(
    raw: bytes,
) -> tuple[str, datetime, datetime, tuple[RegisteredFootballParticipant, ...]]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("participant registry JSON is malformed") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "registry_revision",
        "generated_at_utc",
        "evaluated_at_utc",
        "participants",
    }:
        raise ConfigurationError("participant registry input fields are not exact")
    if document["schema_version"] != PARTICIPANT_REGISTRY_INPUT_SCHEMA:
        raise ConfigurationError("participant registry input schema is unsupported")
    revision = _identifier(document["registry_revision"], "registry_revision")
    generated = _timestamp(document["generated_at_utc"], "generated_at_utc")
    evaluated = _timestamp(document["evaluated_at_utc"], "evaluated_at_utc")
    if generated > evaluated:
        raise ConfigurationError("participant registry generation is after evaluation cutoff")
    rows = document["participants"]
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ConfigurationError("participant registry participants must be a bounded array")
    participants = _validate_rows(rows)
    return revision, generated, evaluated, participants


def write_participant_registry_artifact(
    *,
    root: Path,
    relative_directory: str,
    registry_revision: str,
    generated_at_utc: datetime,
    evaluated_at_utc: datetime,
    participants: tuple[RegisteredFootballParticipant, ...],
) -> AnalyticalArtifact:
    generated = require_utc(generated_at_utc, field_name="generated_at_utc")
    evaluated = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    checked = _validate_rows([item.to_json() for item in participants])
    lineage_ids = sorted({item.source_lineage_artifact_id for item in checked})
    lineage_checksums = sorted({item.source_lineage_checksum_sha256 for item in checked})
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=PARTICIPANT_REGISTRY_ARTIFACT_TYPE,
        schema_version=PARTICIPANT_REGISTRY_SCHEMA,
        payload={
            "source_classification": "verified-reconciled-football-snapshots",
            "generated_at_utc": format_utc_timestamp(generated),
            "evaluated_at_utc": format_utc_timestamp(evaluated),
            "registry_revision": _identifier(registry_revision, "registry_revision"),
            "source_lineage_artifact_ids": cast(JsonValue, lineage_ids),
            "source_lineage_checksums": cast(JsonValue, lineage_checksums),
            "row_count": len(checked),
            "participants": [item.to_json() for item in checked],
        },
    )


def load_participant_registry_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_artifact_id: str | None = None,
    expected_checksum: str | None = None,
) -> FootballParticipantRegistry:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=PARTICIPANT_REGISTRY_ARTIFACT_TYPE,
        expected_schema_version=PARTICIPANT_REGISTRY_SCHEMA,
        expected_artifact_id=expected_artifact_id,
        expected_checksum=expected_checksum,
    )
    payload = require_dict(artifact.payload, field="participant_registry")
    if set(payload) != {
        "source_classification",
        "generated_at_utc",
        "evaluated_at_utc",
        "registry_revision",
        "source_lineage_artifact_ids",
        "source_lineage_checksums",
        "row_count",
        "participants",
    }:
        raise ArtifactError("participant registry payload fields are not exact")
    if payload["source_classification"] != "verified-reconciled-football-snapshots":
        raise ArtifactError("participant registry source classification is invalid")
    try:
        participants = _validate_rows(require_list(payload["participants"], field="participants"))
        generated = _timestamp(payload["generated_at_utc"], "generated_at_utc")
        evaluated = _timestamp(payload["evaluated_at_utc"], "evaluated_at_utc")
        revision = _identifier(payload["registry_revision"], "registry_revision")
    except ConfigurationError as exc:
        raise ArtifactError(str(exc)) from exc
    if generated > evaluated or payload["row_count"] != len(participants):
        raise ArtifactError("participant registry counts or cutoffs are invalid")
    ids = [item.source_lineage_artifact_id for item in participants]
    checksums = [item.source_lineage_checksum_sha256 for item in participants]
    if payload["source_lineage_artifact_ids"] != sorted(set(ids)):
        raise ArtifactError("participant registry artifact lineage is inconsistent")
    if payload["source_lineage_checksums"] != sorted(set(checksums)):
        raise ArtifactError("participant registry checksum lineage is inconsistent")
    return FootballParticipantRegistry(artifact, generated, evaluated, revision, participants)


def _validate_rows(rows: Sequence[object]) -> tuple[RegisteredFootballParticipant, ...]:
    participants = tuple(_parse_row(value, index) for index, value in enumerate(rows))
    if tuple(item.canonical_participant_id for item in participants) != tuple(
        sorted(item.canonical_participant_id for item in participants)
    ):
        raise ConfigurationError("participant registry rows are not canonically ordered")
    if len({item.canonical_participant_id for item in participants}) != len(participants):
        raise ConfigurationError("participant registry contains duplicate canonical identities")
    source_map: dict[tuple[str, str], str] = {}
    for item in participants:
        key = (item.source_name, item.source_participant_id)
        prior = source_map.setdefault(key, item.canonical_participant_id)
        if prior != item.canonical_participant_id:
            raise ConfigurationError("participant registry source identity is contradictory")
    return participants


def _parse_row(value: object, index: int) -> RegisteredFootballParticipant:
    if not isinstance(value, dict) or set(value) != PARTICIPANT_REGISTRY_ROW_FIELDS:
        raise ConfigurationError(f"participant registry row {index} fields are not exact")
    row = cast(dict[str, object], value)
    participant_id = _canonical_participant_id(row["canonical_participant_id"])
    sport = _identifier(row["sport_code"], "sport_code")
    if sport != "football":
        raise ConfigurationError("participant registry sport must be football")
    kind = _identifier(row["participant_kind"], "participant_kind")
    if kind != ParticipantType.TEAM.value:
        raise ConfigurationError("participant registry kind must be team")
    display = _safe_display(row["canonical_display_name"])
    competitions_raw = row["competition_ids"]
    if not isinstance(competitions_raw, list) or not competitions_raw:
        raise ConfigurationError("participant competition_ids must be non-empty")
    competitions = tuple(_identifier(item, "competition_id") for item in competitions_raw)
    if competitions != tuple(sorted(set(competitions))):
        raise ConfigurationError("participant competition_ids must be unique and ordered")
    for competition in competitions:
        try:
            get_competition(competition)
        except Exception as exc:
            raise ConfigurationError("participant competition is not registered") from exc
    state = _identifier(row["reconciliation_state"], "reconciliation_state")
    try:
        ReconciliationState(state)
    except ValueError as exc:
        raise ConfigurationError("participant reconciliation state is invalid") from exc
    if state not in _SAFE_STATES:
        raise ConfigurationError("participant reconciliation state is not downstream safe")
    valid_from = _date(row["valid_from"], "valid_from")
    valid_until = None if row["valid_until"] is None else _date(row["valid_until"], "valid_until")
    if valid_until is not None and valid_until < valid_from:
        raise ConfigurationError("participant validity interval is invalid")
    checksum = _text(row["source_lineage_checksum_sha256"], "source lineage checksum")
    try:
        validate_sha256_checksum(checksum)
    except Exception as exc:
        raise ConfigurationError("participant source lineage checksum is invalid") from exc
    return RegisteredFootballParticipant(
        participant_id,
        sport,
        kind,
        display,
        competitions,
        state,
        _identifier(row["source_name"], "source_name"),
        _identifier(row["source_participant_id"], "source_participant_id"),
        _identifier(row["source_lineage_artifact_id"], "source_lineage_artifact_id"),
        checksum,
        valid_from,
        valid_until,
    )


def _canonical_participant_id(value: object) -> str:
    text = _text(value, "canonical_participant_id")
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ConfigurationError("canonical participant identity is invalid") from exc
    if parsed.version != 5 or str(parsed) != text:
        raise ConfigurationError("canonical participant identity is not canonical UUIDv5")
    return text


def _identifier(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        return validate_identifier(text, field_name=field)
    except Exception as exc:
        raise ConfigurationError(f"{field} is invalid") from exc


def _safe_display(value: object) -> str:
    text = _text(value, "canonical_display_name")
    if _PROHIBITED.search(text):
        raise ConfigurationError("canonical display name contains prohibited material")
    try:
        return validate_display_name(text, field_name="canonical_display_name")
    except Exception as exc:
        raise ConfigurationError("canonical display name is invalid") from exc


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or _PROHIBITED.search(value):
        raise ConfigurationError(f"{field} must be safe non-empty trimmed text")
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = require_str(value, field=field)
    if not text.endswith("Z"):
        raise ConfigurationError(f"{field} must be canonical UTC")
    try:
        result = parse_utc_timestamp(text)
    except Exception as exc:
        raise ConfigurationError(f"{field} is invalid") from exc
    if format_utc_timestamp(result) != text:
        raise ConfigurationError(f"{field} must be canonical UTC")
    return result


def _date(value: object, field: str) -> date:
    text = _text(value, field)
    try:
        result = date.fromisoformat(text)
    except ValueError as exc:
        raise ConfigurationError(f"{field} is invalid") from exc
    if result.isoformat() != text:
        raise ConfigurationError(f"{field} must be canonical ISO date")
    return result

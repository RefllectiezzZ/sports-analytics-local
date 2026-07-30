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

import pyarrow.parquet as pq

from sports_analytics.artifact_strict import require_dict, require_list, require_str
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ConfigurationError
from sports_analytics.data.codec import format_utc_timestamp, parse_utc_timestamp
from sports_analytics.data.types import JsonValue, validate_identifier, validate_sha256_checksum
from sports_analytics.snapshots.paths import resolve_snapshot_file
from sports_analytics.snapshots.reader import SnapshotVerificationResult, verify_snapshot_directory
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sports.contracts import (
    DOWNSTREAM_SAFE_RECONCILIATION_STATES,
    ParticipantType,
    ReconciliationState,
    require_utc,
    validate_display_name,
)
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
)
from sports_analytics.sports.football.schemas import football_snapshot_suite

PARTICIPANT_REGISTRY_ARTIFACT_TYPE: Final[str] = "canonical-football-participant-registry"
PARTICIPANT_REGISTRY_SCHEMA: Final[str] = "canonical-football-participant-registry-v2"
PARTICIPANT_REGISTRY_INPUT_SCHEMA: Final[str] = "canonical-football-participant-registry-request-v2"
PARTICIPANT_SOURCE_ROLE: Final[str] = "football_ingestion_snapshot"
_REFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "role",
        "relative_directory",
        "artifact_id",
        "checksum_sha256",
        "artifact_type",
        "schema_version",
    }
)
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
class ParticipantSourceReference:
    role: str
    relative_directory: str
    artifact_id: str
    checksum_sha256: str
    artifact_type: str
    schema_version: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "role": self.role,
            "relative_directory": self.relative_directory,
            "artifact_id": self.artifact_id,
            "checksum_sha256": self.checksum_sha256,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
        }


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
    source_artifacts: tuple[ParticipantSourceReference, ...]

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
    """Return a safe reference-only production build request."""
    return (
        json.dumps(
            {
                "schema_version": PARTICIPANT_REGISTRY_INPUT_SCHEMA,
                "registry_revision": "reviewed-registry-2026-08-01",
                "evaluated_at_utc": "2026-08-01T12:00:00.000000Z",
                "source_artifacts": [
                    {
                        "role": PARTICIPANT_SOURCE_ROLE,
                        "relative_directory": (
                            "football-ingestion/football-canonical-v2/"
                            "prt-primeira-liga/2025-2026/SNAPSHOT_UUID"
                        ),
                        "artifact_id": "SNAPSHOT_UUID",
                        "checksum_sha256": "0" * 64,
                        "artifact_type": FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                        "schema_version": FOOTBALL_CANONICAL_SCHEMA_VERSION,
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
) -> tuple[str, datetime, tuple[ParticipantSourceReference, ...]]:
    """Parse the reference-only production registry request."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("participant registry JSON is malformed") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "registry_revision",
        "evaluated_at_utc",
        "source_artifacts",
    }:
        raise ConfigurationError("participant registry input fields are not exact")
    if document["schema_version"] != PARTICIPANT_REGISTRY_INPUT_SCHEMA:
        raise ConfigurationError("participant registry input schema is unsupported")
    revision = _identifier(document["registry_revision"], "registry_revision")
    evaluated = _timestamp(document["evaluated_at_utc"], "evaluated_at_utc")
    raw_references = document["source_artifacts"]
    if not isinstance(raw_references, list) or not raw_references or len(raw_references) > 100:
        raise ConfigurationError("participant source_artifacts must be a bounded array")
    references = tuple(_source_reference(item) for item in raw_references)
    if references != tuple(sorted(set(references))):
        raise ConfigurationError("participant source artifacts are not canonical")
    return revision, evaluated, references


def derive_participant_registry_artifact(
    *,
    root: Path,
    source_root: Path,
    relative_directory: str,
    registry_revision: str,
    evaluated_at_utc: datetime,
    source_artifacts: tuple[ParticipantSourceReference, ...],
) -> AnalyticalArtifact:
    """Derive and publish a registry exclusively from strictly verified snapshots."""
    evaluated = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    participants = _derive_participants(source_root=source_root, references=source_artifacts)
    return _write_participant_registry_artifact(
        root=root,
        relative_directory=relative_directory,
        registry_revision=registry_revision,
        generated_at_utc=evaluated,
        evaluated_at_utc=evaluated,
        participants=participants,
        source_artifacts=source_artifacts,
    )


def _write_participant_registry_artifact(
    *,
    root: Path,
    relative_directory: str,
    registry_revision: str,
    generated_at_utc: datetime,
    evaluated_at_utc: datetime,
    participants: tuple[RegisteredFootballParticipant, ...],
    source_artifacts: tuple[ParticipantSourceReference, ...],
) -> AnalyticalArtifact:
    generated = require_utc(generated_at_utc, field_name="generated_at_utc")
    evaluated = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    checked = _validate_rows([item.to_json() for item in participants])
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=PARTICIPANT_REGISTRY_ARTIFACT_TYPE,
        schema_version=PARTICIPANT_REGISTRY_SCHEMA,
        payload={
            "source_classification": "verified-canonical-football-ingestion-snapshots",
            "generated_at_utc": format_utc_timestamp(generated),
            "evaluated_at_utc": format_utc_timestamp(evaluated),
            "registry_revision": _identifier(registry_revision, "registry_revision"),
            "source_artifacts": [item.to_json() for item in source_artifacts],
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
    source_root: Path | None = None,
) -> FootballParticipantRegistry:
    """Load a registry and re-derive it from its exact upstream snapshots."""
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
        "source_artifacts",
        "row_count",
        "participants",
    }:
        raise ArtifactError("participant registry payload fields are not exact")
    if payload["source_classification"] != "verified-canonical-football-ingestion-snapshots":
        raise ArtifactError("participant registry source classification is invalid")
    try:
        participants = _validate_rows(require_list(payload["participants"], field="participants"))
        references = tuple(
            _source_reference(item)
            for item in require_list(payload["source_artifacts"], field="source_artifacts")
        )
        generated = _timestamp(payload["generated_at_utc"], "generated_at_utc")
        evaluated = _timestamp(payload["evaluated_at_utc"], "evaluated_at_utc")
        revision = _identifier(payload["registry_revision"], "registry_revision")
        derived = _derive_participants(
            source_root=root if source_root is None else source_root,
            references=references,
        )
    except (ConfigurationError, Exception) as exc:
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError(str(exc)) from exc
    if generated > evaluated or payload["row_count"] != len(participants):
        raise ArtifactError("participant registry counts or cutoffs are invalid")
    if references != tuple(sorted(set(references))):
        raise ArtifactError("participant registry source references are not canonical")
    if derived != participants:
        raise ArtifactError("participant registry rows do not match verified upstream artifacts")
    return FootballParticipantRegistry(
        artifact, generated, evaluated, revision, participants, references
    )


def _derive_participants(
    *,
    source_root: Path,
    references: tuple[ParticipantSourceReference, ...],
) -> tuple[RegisteredFootballParticipant, ...]:
    if not references:
        raise ConfigurationError("participant source artifacts are required")
    derived: dict[str, RegisteredFootballParticipant] = {}
    source_identities: dict[tuple[str, str], str] = {}
    for reference in references:
        verified = _verify_reference(source_root, reference)
        directory = reference.relative_directory
        canonical_rows = pq.read_table(
            resolve_snapshot_file(source_root, f"{directory}/participants.parquet")
        ).to_pylist()
        source_rows = pq.read_table(
            resolve_snapshot_file(source_root, f"{directory}/source_participants.parquet")
        ).to_pylist()
        reconciliation_rows = pq.read_table(
            resolve_snapshot_file(source_root, f"{directory}/participant_reconciliations.parquet")
        ).to_pylist()
        event_rows = pq.read_table(
            resolve_snapshot_file(source_root, f"{directory}/events.parquet")
        ).to_pylist()
        canonical = {str(row["canonical_participant_id"]): row for row in canonical_rows}
        reconciliations = {
            (str(row["source_name"]), str(row["source_participant_id"])): row
            for row in reconciliation_rows
        }
        if len(reconciliations) != len(reconciliation_rows):
            raise ConfigurationError("duplicate source identity in participant reconciliation")
        partition = dict(verified.partition_keys)
        competition_id = partition.get("competition_id")
        if competition_id is None:
            raise ConfigurationError("participant snapshot has no competition partition")
        try:
            get_competition(competition_id)
        except Exception as exc:
            raise ConfigurationError("participant competition is not registered") from exc
        event_dates: dict[str, list[date]] = {}
        for event in event_rows:
            if event["sport_code"] != "football" or event["competition_id"] != competition_id:
                raise ConfigurationError("participant snapshot event scope is contradictory")
            for field in (
                "home_canonical_participant_id",
                "away_canonical_participant_id",
            ):
                event_dates.setdefault(str(event[field]), []).append(
                    cast(date, event["event_date"])
                )
        for source in source_rows:
            source_name = str(source["source_name"])
            source_id = str(source["source_participant_id"])
            source_key = (source_name, source_id)
            canonical_id = source["canonical_participant_id"]
            if canonical_id is None or str(canonical_id) not in canonical:
                raise ConfigurationError("source participant has no canonical identity")
            canonical_id = str(canonical_id)
            reconciliation = reconciliations.get(source_key)
            if reconciliation is None:
                raise ConfigurationError("source participant reconciliation is absent")
            if (
                reconciliation["source_participant_key"] != source["source_participant_key"]
                or reconciliation["canonical_participant_id"] != canonical_id
            ):
                raise ConfigurationError("source/canonical participant identity mismatch")
            state = str(reconciliation["reconciliation_state"])
            if state not in _SAFE_STATES:
                raise ConfigurationError("participant reconciliation is unresolved or ambiguous")
            canonical_row = canonical[canonical_id]
            if canonical_row["sport_code"] != "football":
                raise ConfigurationError("participant registry sport must be football")
            if (
                canonical_row["participant_type"] != ParticipantType.CLUB.value
                or source["participant_type"] != ParticipantType.CLUB.value
            ):
                raise ConfigurationError("participant registry kind must be club")
            if source["competition_id"] != competition_id:
                raise ConfigurationError("participant competition membership is contradictory")
            dates = event_dates.get(canonical_id)
            if not dates:
                raise ConfigurationError("participant has no verified competition membership")
            prior_source = source_identities.setdefault(source_key, canonical_id)
            if prior_source != canonical_id:
                raise ConfigurationError("participant source identity is contradictory")
            candidate = RegisteredFootballParticipant(
                canonical_id,
                "football",
                ParticipantType.CLUB.value,
                str(canonical_row["display_name"]),
                (competition_id,),
                state,
                source_name,
                source_id,
                reference.artifact_id,
                reference.checksum_sha256,
                min(dates),
                None,
            )
            prior = derived.get(canonical_id)
            if prior is None:
                derived[canonical_id] = candidate
            elif (
                prior.sport_code,
                prior.participant_kind,
                prior.canonical_display_name,
                prior.reconciliation_state,
                prior.source_name,
                prior.source_participant_id,
            ) != (
                candidate.sport_code,
                candidate.participant_kind,
                candidate.canonical_display_name,
                candidate.reconciliation_state,
                candidate.source_name,
                candidate.source_participant_id,
            ):
                raise ConfigurationError("canonical participant evidence is contradictory")
            else:
                derived[canonical_id] = RegisteredFootballParticipant(
                    prior.canonical_participant_id,
                    prior.sport_code,
                    prior.participant_kind,
                    prior.canonical_display_name,
                    tuple(sorted(set(prior.competition_ids + candidate.competition_ids))),
                    prior.reconciliation_state,
                    prior.source_name,
                    prior.source_participant_id,
                    min(prior.source_lineage_artifact_id, candidate.source_lineage_artifact_id),
                    (
                        prior.source_lineage_checksum_sha256
                        if prior.source_lineage_artifact_id <= candidate.source_lineage_artifact_id
                        else candidate.source_lineage_checksum_sha256
                    ),
                    min(prior.valid_from, candidate.valid_from),
                    None,
                )
    return _validate_rows([derived[key].to_json() for key in sorted(derived)])


def _verify_reference(
    source_root: Path, reference: ParticipantSourceReference
) -> SnapshotVerificationResult:
    if (
        reference.role != PARTICIPANT_SOURCE_ROLE
        or reference.artifact_type != FOOTBALL_INGESTION_SNAPSHOT_TYPE
        or reference.schema_version != FOOTBALL_CANONICAL_SCHEMA_VERSION
    ):
        raise ConfigurationError("participant source role, type, or schema is unsupported")
    verified = verify_snapshot_directory(
        snapshots_directory=source_root,
        relative_manifest_path=f"{reference.relative_directory}/manifest.json",
        suite=football_snapshot_suite(),
    )
    if (
        verified.snapshot_id != reference.artifact_id
        or verified.manifest_checksum_sha256 != reference.checksum_sha256
        or verified.snapshot_type != FOOTBALL_INGESTION_SNAPSHOT_TYPE
        or verified.schema_version != FOOTBALL_CANONICAL_SCHEMA_VERSION
    ):
        raise ConfigurationError("participant source artifact identity or checksum mismatch")
    return verified


def _source_reference(value: object) -> ParticipantSourceReference:
    if not isinstance(value, dict) or set(value) != _REFERENCE_FIELDS:
        raise ConfigurationError("participant source reference fields are not exact")
    values = tuple(
        value[name]
        for name in (
            "role",
            "relative_directory",
            "artifact_id",
            "checksum_sha256",
            "artifact_type",
            "schema_version",
        )
    )
    if any(type(item) is not str or not item or item != item.strip() for item in values):
        raise ConfigurationError("participant source reference text is invalid")
    relative = cast(str, values[1])
    if relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
        raise ConfigurationError("participant source reference path is unsafe")
    try:
        validate_sha256_checksum(cast(str, values[3]))
    except Exception as exc:
        raise ConfigurationError("participant source reference checksum is invalid") from exc
    reference = ParticipantSourceReference(*cast(tuple[str, str, str, str, str, str], values))
    if (
        reference.role != PARTICIPANT_SOURCE_ROLE
        or reference.artifact_type != FOOTBALL_INGESTION_SNAPSHOT_TYPE
        or reference.schema_version != FOOTBALL_CANONICAL_SCHEMA_VERSION
    ):
        raise ConfigurationError("participant source role, type, or schema is unsupported")
    return reference


def _validate_rows(rows: Sequence[object]) -> tuple[RegisteredFootballParticipant, ...]:
    participants = tuple(_parse_row(value, index) for index, value in enumerate(rows))
    if not participants:
        raise ConfigurationError("participant registry cannot be empty")
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
    if kind != ParticipantType.CLUB.value:
        raise ConfigurationError("participant registry kind must be club")
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

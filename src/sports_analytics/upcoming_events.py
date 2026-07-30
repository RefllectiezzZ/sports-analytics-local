"""Strict offline operator boundary for immutable upcoming football events."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
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
from sports_analytics.snapshots.paths import is_absolute_path_text
from sports_analytics.sources.football_data_co_uk.catalog import get_competition
from sports_analytics.sports.contracts import EventStatus, require_utc
from sports_analytics.sports.football.identifiers import parse_canonical_season
from sports_analytics.sports.football.participant_registry import FootballParticipantRegistry
from sports_analytics.sports.identifiers import build_canonical_event_id, build_season_id

UPCOMING_EVENT_ARTIFACT_TYPE: Final[str] = "canonical-upcoming-events"
UPCOMING_EVENT_ARTIFACT_SCHEMA: Final[str] = "canonical-upcoming-events-v1"
UPCOMING_EVENT_IMPORT_SCHEMA: Final[str] = "operator-upcoming-events-import-v1"
UPCOMING_EVENT_FIELDS: Final[tuple[str, ...]] = (
    "sport_code",
    "competition_id",
    "season_label",
    "canonical_home_participant_id",
    "canonical_away_participant_id",
    "event_start_utc",
    "event_occurrence_key",
    "event_status",
    "observed_at_utc",
    "source_kind",
    "source_observation_id",
    "neutral_venue",
    "operator_note",
    "import_batch_id",
)
MAX_UPCOMING_EVENTS: Final[int] = 500
MAX_INPUT_BYTES: Final[int] = 1_048_576
MAX_NOTE_LENGTH: Final[int] = 500
_SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {"operator-csv", "operator-json", "operator-reviewed"}
)
_UNSAFE_TEXT = re.compile(
    r"(?i)(?:(?:https?|ftp)://|www\.|authorization|cookie|token|selector|"
    r"user-agent|referer|x-api-key|request[_ -]?headers?|<[a-z][^>]*>|"
    r"(?:^|[\\/])[A-Za-z]:[\\/]|(?:^|[\\/])Users[\\/])"
)


@dataclass(frozen=True, slots=True, order=True)
class UpcomingEvent:
    canonical_event_id: str
    sport_code: str
    competition_id: str
    season_id: str
    season_label: str
    canonical_home_participant_id: str
    canonical_away_participant_id: str
    event_start_utc: datetime
    event_occurrence_key: str
    event_status: str
    observed_at_utc: datetime
    source_kind: str
    source_observation_id: str
    neutral_venue: bool | None
    operator_note: str | None
    import_batch_id: str

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "canonical_event_id": self.canonical_event_id,
            "sport_code": self.sport_code,
            "competition_id": self.competition_id,
            "season_id": self.season_id,
            "season_label": self.season_label,
            "canonical_home_participant_id": self.canonical_home_participant_id,
            "canonical_away_participant_id": self.canonical_away_participant_id,
            "event_start_utc": format_utc_timestamp(self.event_start_utc),
            "event_occurrence_key": self.event_occurrence_key,
            "event_status": self.event_status,
            "observed_at_utc": format_utc_timestamp(self.observed_at_utc),
            "source_kind": self.source_kind,
            "source_observation_id": self.source_observation_id,
            "neutral_venue": self.neutral_venue,
            "operator_note": self.operator_note,
            "import_batch_id": self.import_batch_id,
        }


def upcoming_event_csv_template() -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=UPCOMING_EVENT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(_template_row(source_kind="operator-csv"))
    return output.getvalue()


def upcoming_event_json_template() -> str:
    return (
        json.dumps(
            {
                "schema_version": UPCOMING_EVENT_IMPORT_SCHEMA,
                "events": [_template_row(source_kind="operator-json")],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def parse_upcoming_event_csv(
    raw: bytes, *, evaluated_at_utc: datetime
) -> tuple[UpcomingEvent, ...]:
    text = _decode(raw)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if reader.fieldnames != list(UPCOMING_EVENT_FIELDS):
            raise ConfigurationError("upcoming-event CSV headers are not exact")
        rows = list(reader)
    except csv.Error as exc:
        raise ConfigurationError("upcoming-event CSV is malformed") from exc
    if len(rows) > MAX_UPCOMING_EVENTS:
        raise ConfigurationError("upcoming-event input exceeds row limit")
    return _validate_rows(rows, evaluated_at_utc=evaluated_at_utc)


def parse_upcoming_event_json(
    raw: bytes, *, evaluated_at_utc: datetime
) -> tuple[UpcomingEvent, ...]:
    text = _decode(raw)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("upcoming-event JSON is malformed") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "events"}:
        raise ConfigurationError("upcoming-event JSON fields are not exact")
    if document["schema_version"] != UPCOMING_EVENT_IMPORT_SCHEMA:
        raise ConfigurationError("upcoming-event import schema is unsupported")
    rows = document["events"]
    if not isinstance(rows, list) or len(rows) > MAX_UPCOMING_EVENTS:
        raise ConfigurationError("upcoming-event events must be a bounded array")
    return _validate_rows(rows, evaluated_at_utc=evaluated_at_utc)


def write_upcoming_event_artifact(
    *,
    root: Path,
    relative_directory: str,
    events: tuple[UpcomingEvent, ...],
    evaluated_at_utc: datetime,
    participant_registry: FootballParticipantRegistry,
) -> AnalyticalArtifact:
    if not events:
        raise ConfigurationError("upcoming-event artifact requires at least one event")
    cutoff = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    ordered = tuple(sorted(events, key=lambda item: item.canonical_event_id))
    batches = {item.import_batch_id for item in ordered}
    if len(batches) != 1:
        raise ConfigurationError("upcoming-event artifact requires one import batch")
    _validate_registered_event_participants(ordered, participant_registry)
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=UPCOMING_EVENT_ARTIFACT_TYPE,
        schema_version=UPCOMING_EVENT_ARTIFACT_SCHEMA,
        payload={
            "source_classification": "strict-offline-operator-import",
            "imported_at_utc": format_utc_timestamp(cutoff),
            "evaluated_at_utc": format_utc_timestamp(cutoff),
            "import_batch_id": next(iter(batches)),
            "row_count": len(ordered),
            "participant_registry": {
                "relative_directory": participant_registry.artifact.relative_directory,
                "artifact_id": participant_registry.artifact.artifact_id,
                "checksum_sha256": participant_registry.artifact.checksum_sha256,
                "registry_revision": participant_registry.registry_revision,
                "validated_participant_ids": cast(
                    JsonValue,
                    sorted(
                        {
                            participant_id
                            for event in ordered
                            for participant_id in (
                                event.canonical_home_participant_id,
                                event.canonical_away_participant_id,
                            )
                        }
                    ),
                ),
                "competition_ids": cast(
                    JsonValue, sorted({event.competition_id for event in ordered})
                ),
            },
            "competition_lineage": "fixed-football-data-competition-registry",
            "events": [item.to_json() for item in ordered],
        },
    )


def load_upcoming_event_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
    expected_artifact_id: str | None = None,
) -> tuple[AnalyticalArtifact, tuple[UpcomingEvent, ...]]:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=UPCOMING_EVENT_ARTIFACT_TYPE,
        expected_schema_version=UPCOMING_EVENT_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
        expected_artifact_id=expected_artifact_id,
    )
    payload = require_dict(artifact.payload, field="upcoming_event_artifact")
    if set(payload) != {
        "source_classification",
        "imported_at_utc",
        "evaluated_at_utc",
        "import_batch_id",
        "row_count",
        "participant_registry",
        "competition_lineage",
        "events",
    }:
        raise ArtifactError("upcoming-event artifact payload fields are not exact")
    if payload["source_classification"] != "strict-offline-operator-import":
        raise ArtifactError("upcoming-event source classification is invalid")
    _validate_participant_registry_lineage(payload["participant_registry"])
    imported_at = _artifact_timestamp(payload["imported_at_utc"], "imported_at_utc")
    cutoff = _artifact_timestamp(payload["evaluated_at_utc"], "evaluated_at_utc")
    if imported_at != cutoff:
        raise ArtifactError("upcoming-event import and evaluation cutoffs differ")
    rows = require_list(payload["events"], field="events")
    try:
        events = _validate_artifact_rows(rows, evaluated_at_utc=cutoff)
    except ConfigurationError as exc:
        raise ArtifactError(str(exc)) from exc
    if payload["row_count"] != len(events):
        raise ArtifactError("upcoming-event row count mismatch")
    if payload["import_batch_id"] != events[0].import_batch_id:
        raise ArtifactError("upcoming-event import batch mismatch")
    if tuple(item.canonical_event_id for item in events) != tuple(
        sorted(item.canonical_event_id for item in events)
    ):
        raise ArtifactError("upcoming-event rows are not canonically ordered")
    return artifact, events


def verify_upcoming_event_participant_registry(
    *,
    artifact: AnalyticalArtifact,
    events: tuple[UpcomingEvent, ...],
    participant_registry: FootballParticipantRegistry,
) -> None:
    """Require exact event-to-registry lineage and revalidate every participant."""
    payload = require_dict(artifact.payload, field="upcoming_event_artifact")
    lineage = require_dict(payload.get("participant_registry"), field="participant_registry")
    expected = {
        "relative_directory": participant_registry.artifact.relative_directory,
        "artifact_id": participant_registry.artifact.artifact_id,
        "checksum_sha256": participant_registry.artifact.checksum_sha256,
        "registry_revision": participant_registry.registry_revision,
        "validated_participant_ids": sorted(
            {
                participant_id
                for event in events
                for participant_id in (
                    event.canonical_home_participant_id,
                    event.canonical_away_participant_id,
                )
            }
        ),
        "competition_ids": sorted({event.competition_id for event in events}),
    }
    if lineage != expected:
        raise ArtifactError("upcoming-event participant registry lineage mismatch")
    try:
        _validate_registered_event_participants(events, participant_registry)
    except ConfigurationError as exc:
        raise ArtifactError(str(exc)) from exc


def _validate_participant_registry_lineage(value: object) -> None:
    lineage = require_dict(value, field="participant_registry")
    if set(lineage) != {
        "relative_directory",
        "artifact_id",
        "checksum_sha256",
        "registry_revision",
        "validated_participant_ids",
        "competition_ids",
    }:
        raise ArtifactError("upcoming-event participant registry lineage fields are not exact")
    for field in ("relative_directory", "artifact_id", "checksum_sha256", "registry_revision"):
        if type(lineage[field]) is not str or not lineage[field]:
            raise ArtifactError("upcoming-event participant registry lineage is invalid")
    if is_absolute_path_text(cast(str, lineage["relative_directory"])):
        raise ArtifactError("upcoming-event participant registry path is invalid")
    try:
        validate_sha256_checksum(cast(str, lineage["checksum_sha256"]))
    except Exception as exc:
        raise ArtifactError("upcoming-event participant registry checksum is invalid") from exc
    for field in ("validated_participant_ids", "competition_ids"):
        values = lineage[field]
        if (
            not isinstance(values, list)
            or not values
            or any(type(item) is not str or not item for item in values)
            or cast(list[str], values) != sorted(set(cast(list[str], values)))
        ):
            raise ArtifactError("upcoming-event participant registry lineage is invalid")


def _validate_registered_event_participants(
    events: tuple[UpcomingEvent, ...],
    registry: FootballParticipantRegistry,
) -> None:
    for event in events:
        for participant_id in (
            event.canonical_home_participant_id,
            event.canonical_away_participant_id,
        ):
            registry.require_registered_participant(
                participant_id,
                competition_id=event.competition_id,
                event_date=event.event_start_utc.date(),
            )


def _validate_rows(
    rows: Sequence[object], *, evaluated_at_utc: datetime
) -> tuple[UpcomingEvent, ...]:
    cutoff = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    unique: dict[str, UpcomingEvent] = {}
    for index, value in enumerate(rows):
        event = _parse_row(value, index=index, cutoff=cutoff)
        previous = unique.get(event.canonical_event_id)
        if previous is not None and previous != event:
            raise ConfigurationError("contradictory duplicate upcoming-event identity")
        unique[event.canonical_event_id] = event
    if not unique:
        raise ConfigurationError("upcoming-event input is empty")
    return tuple(sorted(unique.values(), key=lambda item: item.canonical_event_id))


def _validate_artifact_rows(
    rows: Sequence[object], *, evaluated_at_utc: datetime
) -> tuple[UpcomingEvent, ...]:
    import_rows: list[dict[str, object]] = []
    identities: list[tuple[str, str]] = []
    artifact_fields = set(UPCOMING_EVENT_FIELDS) | {"canonical_event_id", "season_id"}
    for index, value in enumerate(rows):
        if not isinstance(value, dict) or set(value) != artifact_fields:
            raise ConfigurationError(f"upcoming event artifact row {index} fields are not exact")
        canonical_event_id = _identifier(value["canonical_event_id"], "canonical_event_id")
        season_id = _identifier(value["season_id"], "season_id")
        import_rows.append({field: value[field] for field in UPCOMING_EVENT_FIELDS})
        identities.append((canonical_event_id, season_id))
    events = _validate_rows(import_rows, evaluated_at_utc=evaluated_at_utc)
    if len(events) != len(identities):
        raise ConfigurationError("upcoming-event artifact contains duplicate identities")
    expected = tuple(sorted((item.canonical_event_id, item.season_id) for item in events))
    if tuple(sorted(identities)) != expected:
        raise ConfigurationError("upcoming-event artifact derived identity mismatch")
    return events


def _parse_row(value: object, *, index: int, cutoff: datetime) -> UpcomingEvent:
    if not isinstance(value, dict) or set(value) != set(UPCOMING_EVENT_FIELDS):
        raise ConfigurationError(f"upcoming event row {index} fields are not exact")
    row = cast(dict[str, object], value)
    sport = _text(row["sport_code"], "sport_code")
    if sport != "football":
        raise ConfigurationError("upcoming-event sport must be football")
    competition = _text(row["competition_id"], "competition_id")
    try:
        get_competition(competition)
    except Exception as exc:
        raise ConfigurationError("upcoming-event competition is not registered") from exc
    season_label = _text(row["season_label"], "season_label")
    try:
        canonical_label, season_start, season_end, _source = parse_canonical_season(season_label)
    except Exception as exc:
        raise ConfigurationError("upcoming-event season is invalid") from exc
    season_id = build_season_id(competition_id=competition, label=canonical_label)
    home = _participant_id(row["canonical_home_participant_id"], "home participant")
    away = _participant_id(row["canonical_away_participant_id"], "away participant")
    if home == away:
        raise ConfigurationError("upcoming-event participants must differ")
    start = _timestamp(row["event_start_utc"], "event_start_utc")
    observed = _timestamp(row["observed_at_utc"], "observed_at_utc")
    if start.year not in {season_start, season_end}:
        raise ConfigurationError("upcoming event is outside its canonical season")
    if observed > cutoff:
        raise ConfigurationError("upcoming-event observation is after analytical cutoff")
    if start <= cutoff:
        raise ConfigurationError("upcoming event must start after analytical cutoff")
    occurrence = _identifier(row["event_occurrence_key"], "event_occurrence_key")
    status = _text(row["event_status"], "event_status")
    if status != EventStatus.SCHEDULED.value:
        raise ConfigurationError("upcoming-event status must be scheduled")
    source_kind = _text(row["source_kind"], "source_kind")
    if source_kind not in _SOURCE_KINDS:
        raise ConfigurationError("upcoming-event source kind is unsupported")
    source_observation_id = _identifier(row["source_observation_id"], "source_observation_id")
    import_batch_id = _identifier(row["import_batch_id"], "import_batch_id")
    neutral = _optional_bool(row["neutral_venue"], "neutral_venue")
    note = _optional_text(row["operator_note"], "operator_note")
    event_id = build_canonical_event_id(
        sport_code=sport,
        competition_id=competition,
        season_id=season_id,
        home_canonical_participant_id=home,
        away_canonical_participant_id=away,
        event_occurrence_key=occurrence,
    )
    return UpcomingEvent(
        canonical_event_id=event_id,
        sport_code=sport,
        competition_id=competition,
        season_id=season_id,
        season_label=canonical_label,
        canonical_home_participant_id=home,
        canonical_away_participant_id=away,
        event_start_utc=start,
        event_occurrence_key=occurrence,
        event_status=status,
        observed_at_utc=observed,
        source_kind=source_kind,
        source_observation_id=source_observation_id,
        neutral_venue=neutral,
        operator_note=note,
        import_batch_id=import_batch_id,
    )


def _template_row(*, source_kind: str) -> dict[str, object]:
    return {
        "sport_code": "football",
        "competition_id": "prt-primeira-liga",
        "season_label": "2026-2027",
        "canonical_home_participant_id": "11111111-1111-5111-8111-111111111111",
        "canonical_away_participant_id": "22222222-2222-5222-8222-222222222222",
        "event_start_utc": "2026-08-15T19:00:00.000000Z",
        "event_occurrence_key": "round-1",
        "event_status": "scheduled",
        "observed_at_utc": "2026-08-01T12:00:00.000000Z",
        "source_kind": source_kind,
        "source_observation_id": "operator-observation-1",
        "neutral_venue": "false" if source_kind == "operator-csv" else False,
        "operator_note": "",
        "import_batch_id": "operator-batch-1",
    }


def _decode(raw: bytes) -> str:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT_BYTES or b"\x00" in raw:
        raise ConfigurationError("upcoming-event input bytes are invalid")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("upcoming-event input must be UTF-8") from exc


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfigurationError(f"{field} must be non-empty trimmed text")
    if len(value) > MAX_NOTE_LENGTH or _UNSAFE_TEXT.search(value) or is_absolute_path_text(value):
        raise ConfigurationError(f"{field} contains prohibited request or path material")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, field)


def _identifier(value: object, field: str) -> str:
    try:
        return validate_identifier(_text(value, field), field_name=field)
    except Exception as exc:
        raise ConfigurationError(f"{field} is invalid") from exc


def _participant_id(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ConfigurationError(f"{field} is unresolved") from exc
    if parsed.version != 5:
        raise ConfigurationError(f"{field} is not a canonical reconciled identity")
    return str(parsed)


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    if not text.endswith("Z"):
        raise ConfigurationError(f"{field} must be canonical UTC ending in Z")
    try:
        parsed = parse_utc_timestamp(text)
    except Exception as exc:
        raise ConfigurationError(f"{field} is invalid") from exc
    if format_utc_timestamp(parsed) != text:
        raise ConfigurationError(f"{field} is not canonical UTC")
    return parsed


def _artifact_timestamp(value: object, field: str) -> datetime:
    try:
        return _timestamp(require_str(value, field=field), field)
    except ConfigurationError as exc:
        raise ArtifactError(str(exc)) from exc


def _optional_bool(value: object, field: str) -> bool | None:
    if value in (None, ""):
        return None
    if type(value) is bool:
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConfigurationError(f"{field} must be boolean or null")

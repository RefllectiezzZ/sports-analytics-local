"""Human-friendly UI inputs composed onto strict existing operator contracts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, cast

from sports_analytics.artifacts import AnalyticalArtifact
from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorEventReference,
    OperatorQuoteCatalogue,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
    validate_operator_quotes,
)
from sports_analytics.core.exceptions import ConfigurationError, ValueEvaluationError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.sports.football.participant_registry import (
    FootballParticipantRegistry,
    RegisteredFootballParticipant,
)
from sports_analytics.upcoming_events import (
    UpcomingEvent,
    parse_upcoming_event_json,
    write_upcoming_event_artifact,
)

MATCH_INPUT_FIELDS: Final[tuple[str, ...]] = (
    "competition",
    "home_team",
    "away_team",
    "scheduled_time",
    "external_source_label",
)
ODDS_INPUT_FIELDS: Final[tuple[str, ...]] = (
    "provider",
    "match",
    "market",
    "outcome",
    "line",
    "decimal_odds",
    "observed_timestamp",
)
SUPPORTED_MANUAL_MARKETS: Final[tuple[str, ...]] = (
    "match-result",
    "double-chance",
    "draw-no-bet",
    "total-goals",
    "both-teams-to-score",
    "total-goals-odd-even",
    "winning-margin",
)


@dataclass(frozen=True, slots=True)
class RowIssue:
    row_number: int
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class MatchOption:
    label: str
    canonical_event_id: str
    competition_id: str
    event_start_utc: datetime
    home_team: str
    away_team: str


@dataclass(frozen=True, slots=True)
class MatchValidation:
    events: tuple[UpcomingEvent, ...]
    issues: tuple[RowIssue, ...]

    @property
    def is_valid(self) -> bool:
        return bool(self.events) and not self.issues


@dataclass(frozen=True, slots=True)
class OddsValidation:
    inputs: tuple[OperatorQuoteInput, ...]
    catalogue: OperatorQuoteCatalogue | None
    issues: tuple[RowIssue, ...]

    @property
    def is_valid(self) -> bool:
        return self.catalogue is not None and not self.issues


def parse_human_match_upload(content: bytes, *, filename: str) -> tuple[dict[str, object], ...]:
    """Parse a bounded human-friendly CSV or JSON upload."""
    if not content or len(content) > 1_048_576 or b"\x00" in content:
        raise ConfigurationError("match upload is empty, oversized, or contains NUL")
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
            if tuple(reader.fieldnames or ()) != MATCH_INPUT_FIELDS:
                raise ConfigurationError("match CSV headers are not exact")
            rows = [cast(dict[str, object], row) for row in reader]
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ConfigurationError("match CSV is malformed") from exc
    elif suffix == ".json":
        try:
            document = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("match JSON is malformed") from exc
        rows_value = document.get("matches") if isinstance(document, dict) else document
        if not isinstance(rows_value, list):
            raise ConfigurationError("match JSON must be an array or a matches object")
        rows = []
        for row in rows_value:
            if not isinstance(row, dict) or set(row) != set(MATCH_INPUT_FIELDS):
                raise ConfigurationError("match JSON row fields are not exact")
            rows.append(cast(dict[str, object], row))
    else:
        raise ConfigurationError("match upload must use .csv or .json")
    if not rows or len(rows) > 500:
        raise ConfigurationError("match upload must contain 1 through 500 rows")
    return tuple(rows)


def validate_human_matches(
    rows: tuple[dict[str, object], ...],
    *,
    registry: FootballParticipantRegistry,
    evaluated_at_utc: datetime,
) -> MatchValidation:
    """Resolve human names, then invoke the strict upcoming-event validator per row."""
    now = _utc(evaluated_at_utc, "evaluated_at_utc")
    # Submission identity is content-addressed so a retry in a later UI session
    # resolves to the same immutable artifact.
    batch = _digest({"rows": rows})
    events: list[UpcomingEvent] = []
    issues: list[RowIssue] = []
    for index, row in enumerate(rows, start=1):
        try:
            if set(row) != set(MATCH_INPUT_FIELDS):
                raise ConfigurationError("match row fields are not exact")
            competition = _text(row["competition"], "competition")
            start = _timestamp(row["scheduled_time"], "scheduled_time")
            home = _participant_by_name(
                registry,
                competition=competition,
                display_name=_text(row["home_team"], "home_team"),
                event_time=start,
            )
            away = _participant_by_name(
                registry,
                competition=competition,
                display_name=_text(row["away_team"], "away_team"),
                event_time=start,
            )
            label = _optional_text(row["external_source_label"], "external_source_label")
            identity = _digest(
                {
                    "competition": competition,
                    "home": home.canonical_participant_id,
                    "away": away.canonical_participant_id,
                    "start": format_utc_timestamp(start),
                    "label": label,
                }
            )
            strict_row = {
                "sport_code": "football",
                "competition_id": competition,
                "season_label": _season_label(start),
                "canonical_home_participant_id": home.canonical_participant_id,
                "canonical_away_participant_id": away.canonical_participant_id,
                "event_start_utc": format_utc_timestamp(start),
                "event_occurrence_key": f"operator-{identity[:24]}",
                "event_status": "scheduled",
                "observed_at_utc": format_utc_timestamp(now),
                "source_kind": "operator-reviewed",
                "source_observation_id": f"ui-match-{identity[:24]}",
                "neutral_venue": None,
                "operator_note": label,
                "import_batch_id": f"ui-batch-{batch[:24]}",
            }
            payload = json.dumps(
                {
                    "schema_version": "operator-upcoming-events-import-v1",
                    "events": [strict_row],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            events.extend(parse_upcoming_event_json(payload, evaluated_at_utc=now))
        except (ConfigurationError, ValueError) as exc:
            issues.append(RowIssue(index, _field_from_error(exc), _safe_error(exc)))
    if issues:
        return MatchValidation((), tuple(issues))
    unique = {item.canonical_event_id: item for item in events}
    if len(unique) != len(events):
        return MatchValidation(
            (),
            (RowIssue(0, "match", "duplicate upcoming match identity"),),
        )
    return MatchValidation(
        tuple(sorted(unique.values(), key=lambda item: item.canonical_event_id)),
        (),
    )


def publish_human_matches(
    validation: MatchValidation,
    *,
    root: Path,
    registry: FootballParticipantRegistry,
    evaluated_at_utc: datetime,
) -> tuple[AnalyticalArtifact, ...]:
    """Publish one strict immutable event artifact per competition."""
    if not validation.is_valid:
        raise ConfigurationError("invalid matches cannot be published")
    published: list[AnalyticalArtifact] = []
    competitions = sorted({item.competition_id for item in validation.events})
    for competition in competitions:
        events = tuple(item for item in validation.events if item.competition_id == competition)
        identity = _digest(
            {
                "competition": competition,
                "events": [item.canonical_event_id for item in events],
                "batch": events[0].import_batch_id,
            }
        )
        relative = f"mvp/upcoming-events/{competition}/{identity}"
        published.append(
            write_upcoming_event_artifact(
                root=root,
                relative_directory=relative,
                events=events,
                evaluated_at_utc=evaluated_at_utc,
                participant_registry=registry,
            )
        )
    return tuple(published)


def build_match_options(
    events: tuple[UpcomingEvent, ...],
    *,
    registry: FootballParticipantRegistry,
) -> tuple[MatchOption, ...]:
    """Build human labels without exposing canonical identifiers as operator input."""
    options: list[MatchOption] = []
    for event in events:
        home = registry.participant(event.canonical_home_participant_id)
        away = registry.participant(event.canonical_away_participant_id)
        if home is None or away is None:
            continue
        label = (
            f"{home.canonical_display_name} v {away.canonical_display_name} · "
            f"{format_utc_timestamp(event.event_start_utc)}"
        )
        options.append(
            MatchOption(
                label,
                event.canonical_event_id,
                event.competition_id,
                event.event_start_utc,
                home.canonical_display_name,
                away.canonical_display_name,
            )
        )
    return tuple(sorted(options, key=lambda item: (item.event_start_utc, item.label)))


def validate_human_odds(
    rows: tuple[dict[str, object], ...],
    *,
    match_options: tuple[MatchOption, ...],
    registered_provider_ids: frozenset[str],
    evaluated_at_utc: datetime,
) -> OddsValidation:
    """Translate manual rows and enforce the existing strict quote validator."""
    now = _utc(evaluated_at_utc, "evaluated_at_utc")
    matches = {item.label: item for item in match_options}
    matches.update({item.canonical_event_id: item for item in match_options})
    inputs: list[OperatorQuoteInput] = []
    issues: list[RowIssue] = []
    batch = _digest({"rows": rows, "evaluated_at_utc": format_utc_timestamp(now)})
    for index, row in enumerate(rows, start=1):
        try:
            if set(row) != set(ODDS_INPUT_FIELDS):
                raise ValueEvaluationError("odds row fields are not exact")
            provider = _text(row["provider"], "provider")
            match_label = _text(row["match"], "match")
            match = matches.get(match_label)
            if match is None:
                raise ValueEvaluationError("mismatched match")
            line_text = _optional_text(row["line"], "line")
            odds_text = _text(row["decimal_odds"], "decimal_odds")
            try:
                line = None if line_text is None else Decimal(line_text)
                odds = Decimal(odds_text)
            except InvalidOperation as exc:
                raise ValueEvaluationError("invalid decimal odd") from exc
            observed_value = row["observed_timestamp"]
            observed = (
                now
                if observed_value is None or observed_value == ""
                else _timestamp(observed_value, "observed_timestamp")
            )
            inputs.append(
                OperatorQuoteInput(
                    provider_id=provider,
                    provider_display_name=provider,
                    sport_code="football",
                    canonical_event_id=match.canonical_event_id,
                    market_family=_text(row["market"], "market"),
                    outcome_key=_text(row["outcome"], "outcome"),
                    line_value=line,
                    market_period="full-match",
                    participant_scope="event",
                    canonical_participant_id=None,
                    overtime_scope=REGULATION_SCOPE,
                    rules_scope=FOOTBALL_RULES_SCOPE,
                    offered_decimal_odds=odds,
                    observed_at_utc=observed,
                    valid_until_utc=None,
                    source_kind=OperatorQuoteSourceKind.MANUAL,
                    operator_note=None,
                    import_batch_id=f"ui-odds-{batch[:24]}",
                )
            )
        except (ValueEvaluationError, ValueError) as exc:
            issues.append(RowIssue(index, _field_from_error(exc), _safe_error(exc)))
    if issues:
        return OddsValidation((), None, tuple(issues))
    event_refs = tuple(
        OperatorEventReference(
            item.canonical_event_id,
            "football",
            item.event_start_utc,
        )
        for item in match_options
    )
    try:
        catalogue = validate_operator_quotes(
            tuple(inputs),
            registered_provider_ids=registered_provider_ids,
            events=event_refs,
            evaluated_at_utc=now,
        )
        if catalogue.incomplete_market_keys:
            raise ValueEvaluationError("missing complete outcomes for offered market")
    except ValueEvaluationError as exc:
        return OddsValidation(
            (),
            None,
            (RowIssue(0, _field_from_error(exc), _safe_error(exc)),),
        )
    return OddsValidation(tuple(inputs), catalogue, ())


def _participant_by_name(
    registry: FootballParticipantRegistry,
    *,
    competition: str,
    display_name: str,
    event_time: datetime,
) -> RegisteredFootballParticipant:
    matches = tuple(
        item
        for item in registry.participants_for_competition(competition)
        if item.canonical_display_name.casefold() == display_name.casefold()
    )
    if len(matches) != 1:
        raise ConfigurationError("team is not uniquely registered for the competition")
    return registry.require_registered_participant(
        matches[0].canonical_participant_id,
        competition_id=competition,
        event_date=event_time.date(),
    )


def _season_label(value: datetime) -> str:
    year = value.year if value.month >= 7 else value.year - 1
    return f"{year:04d}-{year + 1:04d}"


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, field)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _field_from_error(exc: BaseException) -> str:
    message = str(exc).lower()
    for field in (
        "home_team",
        "away_team",
        "scheduled_time",
        "competition",
        "provider",
        "match",
        "market",
        "outcome",
        "line",
        "decimal",
        "timestamp",
    ):
        if field in message:
            return field
    return "row"


def _safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:300] or type(exc).__name__

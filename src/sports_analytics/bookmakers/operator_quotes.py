"""Strict operator-imported current offered quotes.

This is an offline trust boundary.  It accepts canonical CSV, canonical JSON, or
manual rows, validates them against an explicit event/provider registry, and
publishes immutable content-addressed evidence.  It never accepts URLs, request
headers, cookies, selectors, scripts, or tokens.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ValueEvaluationError
from sports_analytics.data.codec import dumps_canonical_json, format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.markets.capabilities import CapabilityState, capability_for
from sports_analytics.markets.contracts import (
    LineType,
    MarketDefinition,
    MarketSelection,
    MarketStatus,
    OddsQuote,
    ProviderType,
    QuotePhase,
    QuoteQualityStatus,
    QuoteTimestampPrecision,
    SelectionStatus,
    validate_decimal_odds,
)
from sports_analytics.markets.identifiers import (
    build_market_key,
    build_quote_observation_id,
    build_quote_series_id,
)
from sports_analytics.sports.contracts import require_utc, validate_domain_identifier
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    complete_market_quote_from_odds_quotes,
)

OPERATOR_QUOTE_ARTIFACT_TYPE: Final[str] = "operator-current-quotes"
OPERATOR_QUOTE_ARTIFACT_SCHEMA: Final[str] = "operator-current-quotes-v1"
OPERATOR_QUOTE_SOURCE_NAME: Final[str] = "operator-import"
FOOTBALL_RULES_SCOPE: Final[str] = "football-standard-v1"
REGULATION_SCOPE: Final[str] = "regulation"
MAX_OPERATOR_QUOTES: Final[int] = 5_000
MAX_TEXT_LENGTH: Final[int] = 500

OPERATOR_QUOTE_FIELDS: Final[tuple[str, ...]] = (
    "provider_id",
    "provider_display_name",
    "sport_code",
    "canonical_event_id",
    "market_family",
    "outcome_key",
    "line_value",
    "market_period",
    "participant_scope",
    "canonical_participant_id",
    "overtime_scope",
    "rules_scope",
    "offered_decimal_odds",
    "observed_at_utc",
    "valid_until_utc",
    "source_kind",
    "operator_note",
    "import_batch_id",
)


class OperatorQuoteSourceKind(StrEnum):
    CANONICAL_CSV = "canonical-csv"
    CANONICAL_JSON = "canonical-json"
    MANUAL = "manual"
    VERIFIED_SOURCE = "verified-source"


@dataclass(frozen=True, slots=True)
class OperatorEventReference:
    canonical_event_id: str
    sport_code: str
    event_start_utc: datetime
    reconciled: bool = True

    def __post_init__(self) -> None:
        if not self.canonical_event_id or not self.sport_code:
            raise ValueEvaluationError("operator event identity must be non-empty")
        object.__setattr__(
            self,
            "event_start_utc",
            require_utc(self.event_start_utc, field_name="event_start_utc"),
        )


@dataclass(frozen=True, slots=True)
class OperatorQuoteInput:
    """One canonical real offered-price observation."""

    provider_id: str
    provider_display_name: str
    sport_code: str
    canonical_event_id: str
    market_family: str
    outcome_key: str
    line_value: Decimal | None
    market_period: str
    participant_scope: str
    canonical_participant_id: str | None
    overtime_scope: str
    rules_scope: str
    offered_decimal_odds: Decimal
    observed_at_utc: datetime
    valid_until_utc: datetime | None
    source_kind: OperatorQuoteSourceKind
    operator_note: str | None = None
    import_batch_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedOperatorQuote:
    """A quote admitted by the offline operator validation boundary."""

    input: OperatorQuoteInput
    odds_quote: OddsQuote
    quote_batch_id: str
    market_complete: bool
    market_probability: float | None

    @property
    def offered_decimal_odds(self) -> Decimal:
        return self.input.offered_decimal_odds


@dataclass(frozen=True, slots=True)
class OperatorQuoteCatalogue:
    """Immutable validated current-price catalogue."""

    quote_batch_id: str
    observed_as_of_utc: datetime
    quotes: tuple[ValidatedOperatorQuote, ...]
    complete_market_keys: tuple[tuple[object, ...], ...]
    incomplete_market_keys: tuple[tuple[object, ...], ...]

    def quote(self, quote_observation_id: str) -> ValidatedOperatorQuote | None:
        for item in self.quotes:
            if item.odds_quote.quote_observation_id == quote_observation_id:
                return item
        return None


@dataclass(frozen=True, slots=True)
class OperatorQuotePolicy:
    maximum_age: timedelta = timedelta(minutes=30)
    maximum_future_clock_skew: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        if self.maximum_age <= timedelta(0):
            raise ValueEvaluationError("operator quote maximum age must be positive")
        if self.maximum_future_clock_skew < timedelta(0):
            raise ValueEvaluationError("future clock skew cannot be negative")


def parse_operator_quote_csv(content: bytes) -> tuple[OperatorQuoteInput, ...]:
    """Parse exact canonical CSV bytes; no dynamic column interpretation."""
    if not content or len(content) > 5_000_000 or b"\x00" in content:
        raise ValueEvaluationError("operator quote CSV is empty, oversized, or contains NUL")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueEvaluationError("operator quote CSV must be UTF-8") from exc
    try:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        if tuple(reader.fieldnames or ()) != OPERATOR_QUOTE_FIELDS:
            raise ValueEvaluationError("operator quote CSV headers are not exact")
        rows = []
        for index, row in enumerate(reader, start=1):
            if index > MAX_OPERATOR_QUOTES:
                raise ValueEvaluationError("operator quote CSV exceeds the row limit")
            if None in row or any(value is None for value in row.values()):
                raise ValueEvaluationError("operator quote CSV row width is invalid")
            rows.append(
                _input_from_mapping(
                    row,
                    expected_source=OperatorQuoteSourceKind.CANONICAL_CSV,
                )
            )
    except csv.Error as exc:
        raise ValueEvaluationError("operator quote CSV is malformed") from exc
    if not rows:
        raise ValueEvaluationError("operator quote CSV contains no rows")
    return tuple(rows)


def parse_operator_quote_json(content: bytes) -> tuple[OperatorQuoteInput, ...]:
    """Parse exact canonical JSON rows."""
    if not content or len(content) > 5_000_000 or b"\x00" in content:
        raise ValueEvaluationError("operator quote JSON is empty, oversized, or contains NUL")
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueEvaluationError("operator quote JSON is malformed") from exc
    if not isinstance(raw, list) or not raw or len(raw) > MAX_OPERATOR_QUOTES:
        raise ValueEvaluationError("operator quote JSON must be a bounded non-empty array")
    rows: list[OperatorQuoteInput] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != set(OPERATOR_QUOTE_FIELDS):
            raise ValueEvaluationError("operator quote JSON row fields are not exact")
        rows.append(
            _input_from_mapping(item, expected_source=OperatorQuoteSourceKind.CANONICAL_JSON)
        )
    return tuple(rows)


def export_operator_quote_csv_template() -> bytes:
    """Return a safe canonical CSV header and one commented-free example row."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(OPERATOR_QUOTE_FIELDS)
    return output.getvalue().encode("utf-8")


def export_operator_quote_json_template() -> bytes:
    """Return one exact safe JSON row with no provider request material."""
    row: dict[str, object] = {
        "provider_id": "registered-provider",
        "provider_display_name": "Registered Provider",
        "sport_code": "football",
        "canonical_event_id": "canonical-event-id",
        "market_family": "match-result",
        "outcome_key": "home",
        "line_value": None,
        "market_period": "full-match",
        "participant_scope": "event",
        "canonical_participant_id": None,
        "overtime_scope": REGULATION_SCOPE,
        "rules_scope": FOOTBALL_RULES_SCOPE,
        "offered_decimal_odds": "2.00",
        "observed_at_utc": "2026-01-01T00:00:00Z",
        "valid_until_utc": None,
        "source_kind": OperatorQuoteSourceKind.CANONICAL_JSON.value,
        "operator_note": None,
        "import_batch_id": "operator-batch-id",
    }
    return (dumps_canonical_json(cast(JsonValue, [row])) + "\n").encode()


def validate_operator_quotes(
    inputs: tuple[OperatorQuoteInput, ...],
    *,
    registered_provider_ids: frozenset[str],
    events: tuple[OperatorEventReference, ...],
    evaluated_at_utc: datetime,
    policy: OperatorQuotePolicy | None = None,
) -> OperatorQuoteCatalogue:
    """Validate canonical current quotes and derive complete-market no-vig values."""
    if not inputs or len(inputs) > MAX_OPERATOR_QUOTES:
        raise ValueEvaluationError("operator quote import must be bounded and non-empty")
    current = require_utc(evaluated_at_utc, field_name="evaluated_at_utc")
    rules = policy or OperatorQuotePolicy()
    event_index = {item.canonical_event_id: item for item in events}
    if len(event_index) != len(events):
        raise ValueEvaluationError("operator event registry contains duplicate identities")
    preliminary: list[tuple[OperatorQuoteInput, OddsQuote, tuple[object, ...]]] = []
    seen: dict[tuple[object, ...], Decimal] = {}
    for item in inputs:
        _validate_input(
            item,
            registered_provider_ids=registered_provider_ids,
            event_index=event_index,
            evaluated_at_utc=current,
            policy=rules,
        )
        identity = _market_group_key(item)
        selection_identity = (*identity, item.outcome_key)
        previous = seen.get(selection_identity)
        if previous is not None:
            if previous != item.offered_decimal_odds:
                raise ValueEvaluationError("contradictory duplicate operator prices")
            raise ValueEvaluationError("duplicate operator quote identity")
        seen[selection_identity] = item.offered_decimal_odds
        preliminary.append((item, _odds_quote(item), identity))
    grouped: dict[tuple[object, ...], list[tuple[OperatorQuoteInput, OddsQuote]]] = {}
    for item, quote, identity in preliminary:
        grouped.setdefault(identity, []).append((item, quote))
    complete_keys: list[tuple[object, ...]] = []
    incomplete_keys: list[tuple[object, ...]] = []
    normalized_probabilities: dict[str, float] = {}
    for identity in sorted(grouped, key=_identity_text):
        rows = grouped[identity]
        expected = _complete_outcomes(str(identity[3]))
        outcomes = {item.outcome_key for item, _ in rows}
        complete = expected is not None and outcomes == expected
        if complete:
            complete_keys.append(identity)
            raw = {
                quote.quote_observation_id: 1.0 / float(item.offered_decimal_odds)
                for item, quote in rows
            }
            total = math.fsum(raw.values())
            for observation_id, value in raw.items():
                normalized_probabilities[observation_id] = value / total
        else:
            incomplete_keys.append(identity)
    identity_payload: dict[str, JsonValue] = {
        "evaluated_at_utc": format_utc_timestamp(current),
        "quotes": [
            _input_payload(item)
            for item, _, _ in sorted(
                preliminary,
                key=lambda row: (
                    row[0].provider_id,
                    row[0].canonical_event_id,
                    row[0].market_family,
                    row[0].outcome_key,
                    "" if row[0].line_value is None else format(row[0].line_value, "f"),
                ),
            )
        ],
    }
    quote_batch_id = hashlib.sha256(
        dumps_canonical_json(identity_payload).encode("utf-8")
    ).hexdigest()
    validated = tuple(
        ValidatedOperatorQuote(
            input=item,
            odds_quote=quote,
            quote_batch_id=quote_batch_id,
            market_complete=identity in complete_keys,
            market_probability=normalized_probabilities.get(quote.quote_observation_id),
        )
        for item, quote, identity in sorted(
            preliminary,
            key=lambda row: row[1].quote_observation_id,
        )
    )
    return OperatorQuoteCatalogue(
        quote_batch_id=quote_batch_id,
        observed_as_of_utc=current,
        quotes=validated,
        complete_market_keys=tuple(complete_keys),
        incomplete_market_keys=tuple(incomplete_keys),
    )


def complete_operator_market_quote(
    catalogue: OperatorQuoteCatalogue,
    *,
    quote_observation_id: str,
) -> CompleteMarketQuote:
    """Return one exact complete offered market containing the selected quote."""
    selected = catalogue.quote(quote_observation_id)
    if selected is None:
        raise ValueEvaluationError("operator quote is not in the validated catalogue")
    if not selected.market_complete:
        raise ValueEvaluationError("operator offered market is incomplete")
    identity = _market_group_key(selected.input)
    quotes = tuple(
        item.odds_quote for item in catalogue.quotes if _market_group_key(item.input) == identity
    )
    return complete_market_quote_from_odds_quotes(quotes)


def operator_catalogue_payload(catalogue: OperatorQuoteCatalogue) -> dict[str, JsonValue]:
    """Serialize verified quote evidence with offered-price terminology."""
    return {
        "quote_batch_id": catalogue.quote_batch_id,
        "observed_as_of_utc": format_utc_timestamp(catalogue.observed_as_of_utc),
        "quotes": [
            {
                **_input_payload(item.input),
                "quote_series_id": item.odds_quote.quote_series_id,
                "quote_observation_id": item.odds_quote.quote_observation_id,
                "market_complete": item.market_complete,
                "market_probability": item.market_probability,
            }
            for item in catalogue.quotes
        ],
        "complete_market_count": len(catalogue.complete_market_keys),
        "incomplete_market_count": len(catalogue.incomplete_market_keys),
        "price_semantics": "real-offered-odds",
    }


def write_operator_quote_artifact(
    *,
    root: Path,
    relative_directory: str,
    catalogue: OperatorQuoteCatalogue,
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=OPERATOR_QUOTE_ARTIFACT_TYPE,
        schema_version=OPERATOR_QUOTE_ARTIFACT_SCHEMA,
        payload=operator_catalogue_payload(catalogue),
    )


def load_operator_quote_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=OPERATOR_QUOTE_ARTIFACT_TYPE,
        expected_schema_version=OPERATOR_QUOTE_ARTIFACT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or payload.get("price_semantics") != "real-offered-odds":
        raise ArtifactError("operator quote artifact price semantics are invalid")
    rows = payload.get("quotes")
    if not isinstance(rows, list) or not rows:
        raise ArtifactError("operator quote artifact must contain verified quote rows")
    if (
        payload.get("quote_batch_id")
        != hashlib.sha256(
            dumps_canonical_json(
                {
                    "evaluated_at_utc": payload.get("observed_as_of_utc"),
                    "quotes": [
                        {field: row.get(field) for field in OPERATOR_QUOTE_FIELDS}
                        for row in sorted(
                            (item for item in rows if isinstance(item, dict)),
                            key=lambda item: (
                                str(item.get("provider_id")),
                                str(item.get("canonical_event_id")),
                                str(item.get("market_family")),
                                str(item.get("outcome_key")),
                                str(item.get("line_value") or ""),
                            ),
                        )
                    ],
                }
            ).encode("utf-8")
        ).hexdigest()
    ):
        raise ArtifactError("operator quote batch identity mismatch")
    return artifact


def _validate_input(
    item: OperatorQuoteInput,
    *,
    registered_provider_ids: frozenset[str],
    event_index: dict[str, OperatorEventReference],
    evaluated_at_utc: datetime,
    policy: OperatorQuotePolicy,
) -> None:
    if item.provider_id not in registered_provider_ids:
        raise ValueEvaluationError("operator quote provider is not registered")
    validate_domain_identifier(item.provider_id, field_name="provider_id")
    validate_domain_identifier(item.sport_code, field_name="sport_code")
    validate_domain_identifier(item.market_family, field_name="market_family")
    validate_domain_identifier(item.outcome_key, field_name="outcome_key")
    _safe_text(item.provider_display_name, "provider_display_name")
    _safe_optional_text(item.operator_note, "operator_note")
    _safe_optional_text(item.import_batch_id, "import_batch_id")
    event = event_index.get(item.canonical_event_id)
    if event is None or not event.reconciled:
        raise ValueEvaluationError("operator quote canonical event is absent or unresolved")
    if event.sport_code != item.sport_code:
        raise ValueEvaluationError("operator quote sport differs from canonical event")
    if evaluated_at_utc >= event.event_start_utc:
        raise ValueEvaluationError("operator quote event has already started")
    observed = require_utc(item.observed_at_utc, field_name="observed_at_utc")
    if observed > evaluated_at_utc + policy.maximum_future_clock_skew:
        raise ValueEvaluationError("operator quote observation is in the future")
    if evaluated_at_utc - observed > policy.maximum_age:
        raise ValueEvaluationError("operator quote is stale")
    if item.valid_until_utc is not None:
        valid_until = require_utc(item.valid_until_utc, field_name="valid_until_utc")
        if valid_until < observed or valid_until < evaluated_at_utc:
            raise ValueEvaluationError("operator quote validity interval has expired")
    capability = capability_for(item.sport_code, item.market_family)
    if capability.offered_price_state is not CapabilityState.SUPPORTED:
        raise ValueEvaluationError("operator quote market family is unsupported")
    if item.market_period != "full-match":
        raise ValueEvaluationError("operator quote period is unsupported")
    if item.overtime_scope != REGULATION_SCOPE or item.rules_scope != FOOTBALL_RULES_SCOPE:
        raise ValueEvaluationError("operator quote settlement rules are unknown")
    if item.participant_scope not in {"event", "home", "away"}:
        raise ValueEvaluationError("operator quote participant scope is unsupported")
    if item.participant_scope == "event" and item.canonical_participant_id is not None:
        raise ValueEvaluationError("event-scoped quote cannot name a participant")
    if item.participant_scope != "event" and not item.canonical_participant_id:
        raise ValueEvaluationError("team-scoped quote requires canonical participant identity")
    requires_line = item.market_family in {
        "total-goals",
        "team-total-goals",
        "result-and-total-goals",
        "double-chance-and-total-goals",
        "result-or-total-goals",
        "btts-or-total-goals",
    }
    if requires_line != (item.line_value is not None):
        raise ValueEvaluationError("operator quote line presence does not match market semantics")
    if item.line_value is not None and (
        not item.line_value.is_finite()
        or item.line_value < 0
        or item.line_value % 1 != Decimal("0.5")
    ):
        raise ValueEvaluationError("operator quote line must be a non-negative half-goal line")
    validate_decimal_odds(item.offered_decimal_odds, field_name="offered_decimal_odds")


def _odds_quote(item: OperatorQuoteInput) -> OddsQuote:
    line_type = LineType.NONE.value if item.line_value is None else LineType.TOTAL.value
    definition = MarketDefinition(
        sport_code=item.sport_code,
        market_family=item.market_family,
        market_key=build_market_key(
            sport_code=item.sport_code,
            market_family=item.market_family,
            variant="operator-canonical",
            market_period=item.market_period,
        ),
        market_period=item.market_period,
        participant_scope=item.participant_scope,
        line_type=line_type,
        line_value=item.line_value,
        canonical_participant_id=item.canonical_participant_id,
    )
    selection = MarketSelection(definition=definition, outcome_key=item.outcome_key)
    series_id = build_quote_series_id(
        canonical_event_id=item.canonical_event_id,
        selection=selection,
        provider_type=ProviderType.BOOKMAKER.value,
        provider_id=item.provider_id,
    )
    source_checksum = hashlib.sha256(
        dumps_canonical_json(_input_payload(item)).encode("utf-8")
    ).hexdigest()
    observation_id = build_quote_observation_id(
        quote_series_id=series_id,
        source_name=OPERATOR_QUOTE_SOURCE_NAME,
        source_event_id=item.canonical_event_id,
        selection=selection,
        provider_type=ProviderType.BOOKMAKER.value,
        provider_id=item.provider_id,
        quote_phase=QuotePhase.CURRENT.value,
        source_observed_at_utc=item.observed_at_utc,
        quoted_at_utc=item.observed_at_utc,
        source_file_sha256=source_checksum,
        source_field="operator-offered-decimal-odds",
    )
    return OddsQuote(
        quote_series_id=series_id,
        quote_observation_id=observation_id,
        canonical_event_id=item.canonical_event_id,
        source_name=OPERATOR_QUOTE_SOURCE_NAME,
        source_event_id=item.canonical_event_id,
        selection=selection,
        provider_type=ProviderType.BOOKMAKER.value,
        provider_id=item.provider_id,
        decimal_odds=item.offered_decimal_odds,
        quote_phase=QuotePhase.CURRENT.value,
        source_observed_at_utc=item.observed_at_utc,
        quoted_at_utc=item.observed_at_utc,
        quote_timestamp_precision=QuoteTimestampPrecision.EXACT.value,
        quote_valid_from_utc=item.observed_at_utc,
        quote_valid_to_utc=item.valid_until_utc,
        market_status=MarketStatus.OPEN.value,
        selection_status=SelectionStatus.ACTIVE.value,
        source_field="operator-offered-decimal-odds",
        quality_status=QuoteQualityStatus.SOURCE_PROVIDED.value,
        quality_reason=None,
        source_file_sha256=source_checksum,
        schema_version="operator-current-quote-v1",
    )


def _input_from_mapping(
    row: dict[str, object],
    *,
    expected_source: OperatorQuoteSourceKind,
) -> OperatorQuoteInput:
    source = _required_text(row.get("source_kind"), "source_kind")
    if source != expected_source.value:
        raise ValueEvaluationError(f"operator quote source_kind must be {expected_source.value}")
    line_text = _optional_text(row.get("line_value"), "line_value")
    odds_text = _required_text(row.get("offered_decimal_odds"), "offered_decimal_odds")
    try:
        line = None if line_text is None else Decimal(line_text)
        odds = Decimal(odds_text)
    except InvalidOperation as exc:
        raise ValueEvaluationError("operator quote contains a malformed Decimal") from exc
    return OperatorQuoteInput(
        provider_id=_required_text(row.get("provider_id"), "provider_id"),
        provider_display_name=_required_text(
            row.get("provider_display_name"),
            "provider_display_name",
        ),
        sport_code=_required_text(row.get("sport_code"), "sport_code"),
        canonical_event_id=_required_text(
            row.get("canonical_event_id"),
            "canonical_event_id",
        ),
        market_family=_required_text(row.get("market_family"), "market_family"),
        outcome_key=_required_text(row.get("outcome_key"), "outcome_key"),
        line_value=line,
        market_period=_required_text(row.get("market_period"), "market_period"),
        participant_scope=_required_text(
            row.get("participant_scope"),
            "participant_scope",
        ),
        canonical_participant_id=_optional_text(
            row.get("canonical_participant_id"),
            "canonical_participant_id",
        ),
        overtime_scope=_required_text(row.get("overtime_scope"), "overtime_scope"),
        rules_scope=_required_text(row.get("rules_scope"), "rules_scope"),
        offered_decimal_odds=odds,
        observed_at_utc=_timestamp(row.get("observed_at_utc"), "observed_at_utc"),
        valid_until_utc=_optional_timestamp(
            row.get("valid_until_utc"),
            "valid_until_utc",
        ),
        source_kind=expected_source,
        operator_note=_optional_text(row.get("operator_note"), "operator_note"),
        import_batch_id=_optional_text(row.get("import_batch_id"), "import_batch_id"),
    )


def _input_payload(item: OperatorQuoteInput) -> dict[str, JsonValue]:
    return {
        "provider_id": item.provider_id,
        "provider_display_name": item.provider_display_name,
        "sport_code": item.sport_code,
        "canonical_event_id": item.canonical_event_id,
        "market_family": item.market_family,
        "outcome_key": item.outcome_key,
        "line_value": None if item.line_value is None else format(item.line_value, "f"),
        "market_period": item.market_period,
        "participant_scope": item.participant_scope,
        "canonical_participant_id": item.canonical_participant_id,
        "overtime_scope": item.overtime_scope,
        "rules_scope": item.rules_scope,
        "offered_decimal_odds": format(item.offered_decimal_odds, "f"),
        "observed_at_utc": format_utc_timestamp(item.observed_at_utc),
        "valid_until_utc": (
            None if item.valid_until_utc is None else format_utc_timestamp(item.valid_until_utc)
        ),
        "source_kind": item.source_kind.value,
        "operator_note": item.operator_note,
        "import_batch_id": item.import_batch_id,
    }


def _market_group_key(item: OperatorQuoteInput) -> tuple[object, ...]:
    return (
        item.provider_id,
        item.canonical_event_id,
        item.sport_code,
        item.market_family,
        item.market_period,
        item.participant_scope,
        item.canonical_participant_id,
        item.line_value,
        item.overtime_scope,
        item.rules_scope,
        item.observed_at_utc,
    )


def _complete_outcomes(family: str) -> frozenset[str] | None:
    return {
        "match-result": frozenset({"home", "draw", "away"}),
        "double-chance": frozenset({"home-or-draw", "home-or-away", "draw-or-away"}),
        "draw-no-bet": frozenset({"home", "away"}),
        "total-goals": frozenset({"over", "under"}),
        "both-teams-to-score": frozenset({"yes", "no"}),
        "team-total-goals": frozenset({"over", "under"}),
        "total-goals-odd-even": frozenset({"odd", "even"}),
        "winning-margin": frozenset(
            {
                "draw",
                "home-by-1",
                "home-by-2",
                "home-by-3-plus",
                "away-by-1",
                "away-by-2",
                "away-by-3-plus",
            }
        ),
    }.get(family)


def _identity_text(value: tuple[object, ...]) -> str:
    return "|".join("" if item is None else str(item) for item in value)


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueEvaluationError(f"operator quote {field} must be non-empty text")
    _safe_text(value, field)
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _required_text(value, field)


def _safe_text(value: str, field: str) -> None:
    if len(value) > MAX_TEXT_LENGTH or any(marker in value for marker in ("://", "\r", "\n")):
        raise ValueEvaluationError(f"operator quote {field} contains forbidden text")


def _safe_optional_text(value: str | None, field: str) -> None:
    if value is not None:
        _safe_text(value, field)


def _timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    if not (text.endswith("Z") or text.endswith("+00:00")):
        raise ValueEvaluationError(f"operator quote {field} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueEvaluationError(f"operator quote {field} is invalid") from exc
    return require_utc(parsed, field_name=field)


def _optional_timestamp(value: object, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    return _timestamp(value, field)

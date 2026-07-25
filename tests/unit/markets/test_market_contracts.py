"""Generic market contract tests, including a synthetic line market.

The production adapter only emits football 1X2 quotes. The synthetic totals
market below exists to prove the canonical contract is not structurally limited
to 1X2, without adding a production adapter for it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.markets.contracts import (
    KNOWN_MARKET_FAMILIES,
    KNOWN_MARKET_PERIODS,
    LineType,
    MarketDefinition,
    MarketSelection,
    MarketStatus,
    OddsQuote,
    ParticipantScope,
    ProviderType,
    QuotePhase,
    QuoteQualityStatus,
    QuoteTimestampPrecision,
    SelectionStatus,
    validate_decimal_odds,
    validate_line_value,
)
from sports_analytics.markets.identifiers import build_market_key, build_quote_id
from sports_analytics.markets.schemas import (
    market_quote_rows,
    market_quotes_schema,
    quote_sort_key,
)
from sports_analytics.sports.football.markets import (
    MARKET_KEY_MATCH_RESULT_1X2,
    match_result_1x2_selection,
)
from sports_analytics.sports.identifiers import SPORT_FOOTBALL

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
QUOTED_AT = datetime(2024, 1, 15, 11, 30, tzinfo=UTC)
CANONICAL_EVENT_ID = "1a9f6458-3f3b-53f3-bd63-3d908acd41f6"
SOURCE_EVENT_ID = "3b638feb-1e83-54fd-abc3-0ffffb0ebc08"
CANONICAL_PARTICIPANT_ID = "8bd4c0c4-6d21-5f3f-9e64-1c9a1e5f6a77"
SCHEMA_VERSION = "football-canonical-v2"
RAW_SHA = "a" * 64


def _totals_definition(line: str = "2.5") -> MarketDefinition:
    """Return a synthetic total goals over/under market definition."""
    return MarketDefinition(
        sport_code=SPORT_FOOTBALL,
        market_family="totals",
        market_key=build_market_key(
            sport_code=SPORT_FOOTBALL,
            market_family="totals",
            variant="goals",
            market_period="full-match",
        ),
        market_period="full-match",
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.TOTAL.value,
        line_value=Decimal(line),
        canonical_participant_id=None,
    )


def _quote(
    selection: MarketSelection,
    *,
    decimal_odds: str = "1.9000",
    quote_phase: str = QuotePhase.CURRENT.value,
    quoted_at_utc: datetime | None = QUOTED_AT,
    precision: str = QuoteTimestampPrecision.MINUTE.value,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    market_status: str = MarketStatus.OPEN.value,
    selection_status: str = SelectionStatus.ACTIVE.value,
    quality_status: str = QuoteQualityStatus.SOURCE_PROVIDED.value,
    quality_reason: str | None = None,
    provider_type: str = ProviderType.BOOKMAKER.value,
    provider_id: str = "fixture-bookmaker",
    source_field: str | None = None,
) -> OddsQuote:
    return OddsQuote(
        quote_id=build_quote_id(
            canonical_event_id=CANONICAL_EVENT_ID,
            selection=selection,
            provider_type=provider_type,
            provider_id=provider_id,
            quote_phase=quote_phase,
            source_field=source_field,
        ),
        canonical_event_id=CANONICAL_EVENT_ID,
        source_event_id=SOURCE_EVENT_ID,
        selection=selection,
        provider_type=provider_type,
        provider_id=provider_id,
        decimal_odds=Decimal(decimal_odds),
        quote_phase=quote_phase,
        source_observed_at_utc=OBSERVED_AT,
        quoted_at_utc=quoted_at_utc,
        quote_timestamp_precision=precision,
        quote_valid_from_utc=valid_from,
        quote_valid_to_utc=valid_to,
        market_status=market_status,
        selection_status=selection_status,
        source_field=source_field,
        quality_status=quality_status,
        quality_reason=quality_reason,
        source_file_sha256=RAW_SHA,
        schema_version=SCHEMA_VERSION,
    )


def test_known_taxonomy_registry_documents_extensible_dimensions() -> None:
    assert "match-result" in KNOWN_MARKET_FAMILIES
    assert "totals" in KNOWN_MARKET_FAMILIES
    assert "asian-handicap" in KNOWN_MARKET_FAMILIES
    assert "player-props" in KNOWN_MARKET_FAMILIES
    assert "full-match" in KNOWN_MARKET_PERIODS
    assert "set-1" in KNOWN_MARKET_PERIODS


def test_unregistered_market_family_is_allowed_when_dimensions_are_valid() -> None:
    definition = MarketDefinition(
        sport_code="tennis",
        market_family="future-exotic-market",
        market_key="tennis.future-exotic-market.variant.set-1",
        market_period="set-1",
        participant_scope=ParticipantScope.EVENT.value,
        line_type=LineType.NONE.value,
        line_value=None,
        canonical_participant_id=None,
    )

    assert definition.market_family not in KNOWN_MARKET_FAMILIES
    assert definition.line_value is None


def test_football_1x2_selection_uses_the_generic_contract() -> None:
    selection = match_result_1x2_selection("home")

    assert selection.definition.market_key == MARKET_KEY_MATCH_RESULT_1X2
    assert selection.definition.market_family == "match-result"
    assert selection.definition.market_period == "full-match"
    assert selection.definition.participant_scope == ParticipantScope.EVENT.value
    assert selection.definition.line_type == LineType.NONE.value
    assert selection.definition.line_value is None
    assert selection.source_market_id is None
    assert selection.source_selection_id is None


def test_totals_over_under_market_round_trips_through_parquet(tmp_path: Path) -> None:
    definition = _totals_definition()
    over = _quote(MarketSelection(definition=definition, outcome_key="over"))
    under = _quote(
        MarketSelection(definition=definition, outcome_key="under"),
        decimal_odds="2.0500",
    )

    schema = market_quotes_schema(schema_version=SCHEMA_VERSION)
    table = pa.Table.from_pylist(market_quote_rows((over, under)), schema=schema)
    path = tmp_path / "market_quotes.parquet"
    pq.write_table(table, path)
    read_back = pq.read_table(path)

    assert read_back.schema == schema
    assert read_back.num_rows == 2
    rows = read_back.to_pylist()
    assert {row["outcome_key"] for row in rows} == {"over", "under"}
    assert {row["line_type"] for row in rows} == {"total"}
    assert {row["line_value"] for row in rows} == {Decimal("2.50")}
    assert {row["market_family"] for row in rows} == {"totals"}
    assert rows[0]["decimal_odds"] == Decimal("1.9000")


def test_null_and_non_null_line_values_share_one_dataset(tmp_path: Path) -> None:
    outright = _quote(match_result_1x2_selection("home"))
    totals = _quote(MarketSelection(definition=_totals_definition(), outcome_key="over"))

    schema = market_quotes_schema(schema_version=SCHEMA_VERSION)
    table = pa.Table.from_pylist(market_quote_rows((outright, totals)), schema=schema)
    path = tmp_path / "mixed.parquet"
    pq.write_table(table, path)
    rows = pq.read_table(path).to_pylist()

    by_family = {row["market_family"]: row for row in rows}
    assert by_family["match-result"]["line_value"] is None
    assert by_family["match-result"]["line_type"] == "none"
    assert by_family["totals"]["line_value"] == Decimal("2.50")


def test_line_value_required_for_line_markets() -> None:
    with pytest.raises(NormalizationError, match="line_value is required"):
        MarketDefinition(
            sport_code=SPORT_FOOTBALL,
            market_family="totals",
            market_key="football.totals.goals.full-match",
            market_period="full-match",
            participant_scope=ParticipantScope.EVENT.value,
            line_type=LineType.TOTAL.value,
            line_value=None,
            canonical_participant_id=None,
        )


def test_line_value_rejected_for_outright_markets() -> None:
    with pytest.raises(NormalizationError, match="line_value must be null"):
        MarketDefinition(
            sport_code=SPORT_FOOTBALL,
            market_family="match-result",
            market_key=MARKET_KEY_MATCH_RESULT_1X2,
            market_period="full-match",
            participant_scope=ParticipantScope.EVENT.value,
            line_type=LineType.NONE.value,
            line_value=Decimal("2.5"),
            canonical_participant_id=None,
        )


def test_participant_scoped_market_requires_a_participant() -> None:
    with pytest.raises(NormalizationError, match="canonical_participant_id is required"):
        MarketDefinition(
            sport_code=SPORT_FOOTBALL,
            market_family="team-totals",
            market_key="football.team-totals.goals.full-match",
            market_period="full-match",
            participant_scope=ParticipantScope.TEAM.value,
            line_type=LineType.TOTAL.value,
            line_value=Decimal("1.5"),
            canonical_participant_id=None,
        )


def test_event_scoped_market_must_not_name_a_participant() -> None:
    with pytest.raises(NormalizationError, match="must not name a participant"):
        MarketDefinition(
            sport_code=SPORT_FOOTBALL,
            market_family="match-result",
            market_key=MARKET_KEY_MATCH_RESULT_1X2,
            market_period="full-match",
            participant_scope=ParticipantScope.EVENT.value,
            line_type=LineType.NONE.value,
            line_value=None,
            canonical_participant_id=CANONICAL_PARTICIPANT_ID,
        )


def test_player_market_is_representable() -> None:
    definition = MarketDefinition(
        sport_code=SPORT_FOOTBALL,
        market_family="player-props",
        market_key="football.player-props.shots.full-match",
        market_period="full-match",
        participant_scope=ParticipantScope.PLAYER.value,
        line_type=LineType.TOTAL.value,
        line_value=Decimal("1.5"),
        canonical_participant_id=CANONICAL_PARTICIPANT_ID,
    )
    selection = MarketSelection(
        definition=definition,
        outcome_key="over",
        source_market_id="market-77",
        source_selection_id="selection-91",
    )

    assert selection.source_market_id == "market-77"
    assert selection.source_selection_id == "selection-91"
    assert definition.canonical_participant_id == CANONICAL_PARTICIPANT_ID


@pytest.mark.parametrize("value", ["1.0000", "0.5000", "-2.0000", "1000000"])
def test_invalid_decimal_odds_rejected(value: str) -> None:
    with pytest.raises(NormalizationError):
        validate_decimal_odds(Decimal(value))


def test_non_finite_decimal_odds_rejected() -> None:
    with pytest.raises(NormalizationError, match="finite"):
        validate_decimal_odds(Decimal("NaN"))


def test_line_value_magnitude_is_bounded() -> None:
    with pytest.raises(NormalizationError, match="magnitude"):
        validate_line_value(Decimal("1000"))


def test_negative_handicap_line_is_allowed() -> None:
    assert validate_line_value(Decimal("-1.5")) == Decimal("-1.50")


def test_quote_timestamp_precision_must_match_quoted_at() -> None:
    selection = match_result_1x2_selection("home")
    with pytest.raises(NormalizationError, match="claims a known quote time"):
        _quote(selection, quoted_at_utc=None, precision=QuoteTimestampPrecision.EXACT.value)
    with pytest.raises(NormalizationError, match="observation-only precision"):
        _quote(
            selection,
            quoted_at_utc=QUOTED_AT,
            precision=QuoteTimestampPrecision.SNAPSHOT_OBSERVATION_ONLY.value,
        )


def test_observation_only_quote_keeps_observation_and_quote_time_distinct() -> None:
    quote = _quote(
        match_result_1x2_selection("home"),
        quoted_at_utc=None,
        precision=QuoteTimestampPrecision.SNAPSHOT_OBSERVATION_ONLY.value,
        quote_phase=QuotePhase.OPENING.value,
    )

    assert quote.quoted_at_utc is None
    assert quote.source_observed_at_utc == OBSERVED_AT
    assert quote.quote_timestamp_precision == "snapshot-observation-only"


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(NormalizationError, match="timezone-aware"):
        _quote(
            match_result_1x2_selection("home"),
            quoted_at_utc=datetime(2024, 1, 15, 11, 30),  # noqa: DTZ001 - deliberate
        )


def test_validity_window_order_is_enforced() -> None:
    with pytest.raises(NormalizationError, match="must not precede"):
        _quote(
            match_result_1x2_selection("home"),
            valid_from=OBSERVED_AT,
            valid_to=OBSERVED_AT.replace(hour=10),
        )


def test_validity_window_is_optional_and_typed() -> None:
    quote = _quote(
        match_result_1x2_selection("home"),
        valid_from=OBSERVED_AT,
        valid_to=OBSERVED_AT.replace(hour=13),
    )

    assert quote.quote_valid_from_utc == OBSERVED_AT
    assert quote.quote_valid_to_utc == OBSERVED_AT.replace(hour=13)


def test_market_and_selection_status_are_retained_for_eligibility_filters() -> None:
    quote = _quote(
        match_result_1x2_selection("home"),
        market_status=MarketStatus.SUSPENDED.value,
        selection_status=SelectionStatus.SUSPENDED.value,
    )

    assert quote.market_status == "suspended"
    assert quote.selection_status == "suspended"


def test_caution_quality_requires_a_reason() -> None:
    with pytest.raises(NormalizationError, match="caution quality status requires"):
        _quote(
            match_result_1x2_selection("home"),
            quality_status=QuoteQualityStatus.CAUTION.value,
            quality_reason=None,
        )


def test_unknown_enum_values_are_rejected() -> None:
    selection = match_result_1x2_selection("home")
    with pytest.raises(NormalizationError, match="provider_type must be one of"):
        _quote(selection, provider_type="mystery-provider")
    with pytest.raises(NormalizationError, match="quote_phase must be one of"):
        _quote(selection, quote_phase="halftime")


def test_quote_ids_separate_providers_phases_and_outcomes() -> None:
    selection = match_result_1x2_selection("home")
    base = _quote(selection, quote_phase=QuotePhase.OPENING.value)
    closing = _quote(selection, quote_phase=QuotePhase.CLOSING.value)
    other_provider = _quote(selection, provider_id="other-bookmaker")
    other_outcome = _quote(match_result_1x2_selection("away"))
    totals = _quote(MarketSelection(definition=_totals_definition(), outcome_key="over"))
    other_line = _quote(MarketSelection(definition=_totals_definition("3.5"), outcome_key="over"))

    ids = {
        base.quote_id,
        closing.quote_id,
        other_provider.quote_id,
        other_outcome.quote_id,
        totals.quote_id,
        other_line.quote_id,
    }
    assert len(ids) == 6


def test_quote_sort_key_is_deterministic_and_line_aware() -> None:
    totals = _quote(MarketSelection(definition=_totals_definition(), outcome_key="over"))
    outright = _quote(match_result_1x2_selection("home"))

    ordered = sorted((totals, outright), key=quote_sort_key)

    assert [item.selection.definition.market_family for item in ordered] == [
        "match-result",
        "totals",
    ]
    assert quote_sort_key(totals) == quote_sort_key(totals)

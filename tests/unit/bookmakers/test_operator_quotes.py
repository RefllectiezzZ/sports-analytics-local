from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorEventReference,
    OperatorQuoteInput,
    OperatorQuotePolicy,
    OperatorQuoteSourceKind,
    complete_operator_market_quote,
    parse_operator_quote_csv,
    validate_operator_quotes,
)
from sports_analytics.core.exceptions import ValueEvaluationError

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _quote(outcome: str, odds: str) -> OperatorQuoteInput:
    return OperatorQuoteInput(
        provider_id="local-book",
        provider_display_name="Local Book",
        sport_code="football",
        canonical_event_id="event-1",
        market_family="match-result",
        outcome_key=outcome,
        line_value=None,
        market_period="full-match",
        participant_scope="event",
        canonical_participant_id=None,
        overtime_scope=REGULATION_SCOPE,
        rules_scope=FOOTBALL_RULES_SCOPE,
        offered_decimal_odds=Decimal(odds),
        observed_at_utc=NOW,
        valid_until_utc=NOW + timedelta(minutes=10),
        source_kind=OperatorQuoteSourceKind.MANUAL,
    )


def _validate(rows: tuple[OperatorQuoteInput, ...]):
    return validate_operator_quotes(
        rows,
        registered_provider_ids=frozenset({"local-book"}),
        events=(
            OperatorEventReference(
                canonical_event_id="event-1",
                sport_code="football",
                event_start_utc=NOW + timedelta(days=1),
            ),
        ),
        evaluated_at_utc=NOW,
    )


def test_complete_operator_market_is_devigged_and_offered() -> None:
    catalogue = _validate((_quote("home", "2.20"), _quote("draw", "3.60"), _quote("away", "4.00")))
    assert len(catalogue.complete_market_keys) == 1
    assert sum(item.market_probability or 0.0 for item in catalogue.quotes) == pytest.approx(1.0)
    market = complete_operator_market_quote(
        catalogue,
        quote_observation_id=catalogue.quotes[0].odds_quote.quote_observation_id,
    )
    assert market.quote_phase == "current"
    assert market.provider_id == "local-book"


def test_stale_started_duplicate_and_unknown_rules_are_rejected() -> None:
    stale = replace(_quote("home", "2.20"), observed_at_utc=NOW - timedelta(hours=2))
    with pytest.raises(ValueEvaluationError, match="stale"):
        _validate((stale,))
    with pytest.raises(ValueEvaluationError, match="duplicate"):
        _validate((_quote("home", "2.20"), _quote("home", "2.20")))
    with pytest.raises(ValueEvaluationError, match="rules"):
        _validate((replace(_quote("home", "2.20"), rules_scope="unknown"),))


def test_csv_loader_requires_exact_schema_and_decimal() -> None:
    with pytest.raises(ValueEvaluationError, match="headers"):
        parse_operator_quote_csv(b"provider_id,odds\nbook,2.0\n")
    with pytest.raises(ValueEvaluationError, match="Decimal"):
        row = _quote("home", "2.20")
        from sports_analytics.bookmakers.operator_quotes import OPERATOR_QUOTE_FIELDS

        values = {field: "" for field in OPERATOR_QUOTE_FIELDS}
        values.update(
            provider_id=row.provider_id,
            provider_display_name=row.provider_display_name,
            sport_code=row.sport_code,
            canonical_event_id=row.canonical_event_id,
            market_family=row.market_family,
            outcome_key=row.outcome_key,
            market_period=row.market_period,
            participant_scope=row.participant_scope,
            overtime_scope=row.overtime_scope,
            rules_scope=row.rules_scope,
            offered_decimal_odds="not-a-number",
            observed_at_utc=NOW.isoformat(),
            source_kind="canonical-csv",
        )
        import csv
        import io

        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=OPERATOR_QUOTE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(values)
        parse_operator_quote_csv(stream.getvalue().encode())


def test_policy_rejects_non_positive_freshness() -> None:
    with pytest.raises(ValueEvaluationError, match="positive"):
        OperatorQuotePolicy(maximum_age=timedelta(0))

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.mvp.operator_inputs import (
    MATCH_INPUT_FIELDS,
    ODDS_INPUT_FIELDS,
    build_match_options,
    validate_human_matches,
    validate_human_odds,
)
from tests.helpers_snapshots import build_verified_participant_registry

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _registry(tmp_path: Path):
    _artifact, registry, _reference = build_verified_participant_registry(
        tmp_path,
        root=tmp_path,
        canonical_participant_ids=(
            "11111111-1111-5111-8111-111111111111",
            "22222222-2222-5222-8222-222222222222",
        ),
        relative_directory="registry",
        evaluated_at_utc=NOW,
    )
    return registry


def test_human_matches_resolve_registry_without_operator_ids(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    teams = registry.participants_for_competition("prt-primeira-liga")
    row = {
        "competition": "prt-primeira-liga",
        "home_team": teams[0].canonical_display_name,
        "away_team": teams[1].canonical_display_name,
        "scheduled_time": "2026-08-15T19:00:00Z",
        "external_source_label": "operator fixture",
    }

    validation = validate_human_matches((row,), registry=registry, evaluated_at_utc=NOW)

    assert validation.is_valid
    assert set(row) == set(MATCH_INPUT_FIELDS)
    assert not {"canonical_event_id", "artifact_id", "checksum_sha256"} & set(row)
    assert validation.events[0].event_start_utc > NOW
    retry = validate_human_matches(
        (row,),
        registry=registry,
        evaluated_at_utc=NOW + timedelta(minutes=1),
    )
    assert retry.events[0].canonical_event_id == validation.events[0].canonical_event_id
    assert retry.events[0].import_batch_id == validation.events[0].import_batch_id


def test_human_matches_reject_unregistered_and_post_kickoff_rows(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    team = registry.participants[0].canonical_display_name
    unregistered = {
        "competition": "prt-primeira-liga",
        "home_team": team,
        "away_team": "Unregistered FC",
        "scheduled_time": "2026-08-15T19:00:00Z",
        "external_source_label": "",
    }
    past = {
        **unregistered,
        "away_team": registry.participants[1].canonical_display_name,
        "scheduled_time": "2026-07-31T19:00:00Z",
    }

    assert validate_human_matches((unregistered,), registry=registry, evaluated_at_utc=NOW).issues
    assert validate_human_matches((past,), registry=registry, evaluated_at_utc=NOW).issues


def test_complete_manual_market_uses_strict_quote_validator(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    teams = registry.participants_for_competition("prt-primeira-liga")
    matches = validate_human_matches(
        (
            {
                "competition": "prt-primeira-liga",
                "home_team": teams[0].canonical_display_name,
                "away_team": teams[1].canonical_display_name,
                "scheduled_time": "2026-08-15T19:00:00Z",
                "external_source_label": "",
            },
        ),
        registry=registry,
        evaluated_at_utc=NOW,
    )
    options = build_match_options(matches.events, registry=registry)
    rows = tuple(
        {
            "provider": "betano-pt",
            "match": options[0].label,
            "market": "match-result",
            "outcome": outcome,
            "line": "",
            "decimal_odds": odd,
            "observed_timestamp": "2026-08-01T12:00:00Z",
        }
        for outcome, odd in (("home", "2.10"), ("draw", "3.40"), ("away", "3.60"))
    )

    validation = validate_human_odds(
        rows,
        match_options=options,
        registered_provider_ids=frozenset({"betano-pt"}),
        evaluated_at_utc=NOW,
    )

    assert validation.is_valid
    assert len(validation.inputs) == 3
    assert set(rows[0]) == set(ODDS_INPUT_FIELDS)

    stale = tuple(
        {
            **row,
            "observed_timestamp": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    )
    assert validate_human_odds(
        stale,
        match_options=options,
        registered_provider_ids=frozenset({"betano-pt"}),
        evaluated_at_utc=NOW,
    ).issues

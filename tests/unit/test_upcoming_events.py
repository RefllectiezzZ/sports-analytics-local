from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sports_analytics.artifacts import build_analytical_artifact_document
from sports_analytics.core.exceptions import ArtifactError, ConfigurationError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.services import engine_cli
from sports_analytics.upcoming_events import (
    UPCOMING_EVENT_ARTIFACT_SCHEMA,
    UPCOMING_EVENT_ARTIFACT_TYPE,
    load_upcoming_event_artifact,
    parse_upcoming_event_csv,
    parse_upcoming_event_json,
    upcoming_event_csv_template,
    upcoming_event_json_template,
    write_upcoming_event_artifact,
)

CUTOFF = datetime(2026, 8, 1, 12, tzinfo=UTC)


def test_exact_templates_valid_import_and_strict_reload(tmp_path) -> None:
    csv_text = upcoming_event_csv_template()
    json_text = upcoming_event_json_template()
    assert csv_text.splitlines()[0].startswith("sport_code,competition_id,season_label")
    assert json.loads(json_text)["schema_version"] == "operator-upcoming-events-import-v1"
    csv_events = parse_upcoming_event_csv(csv_text.encode(), evaluated_at_utc=CUTOFF)
    json_events = parse_upcoming_event_json(json_text.encode(), evaluated_at_utc=CUTOFF)
    assert csv_events[0].canonical_event_id == json_events[0].canonical_event_id
    artifact = write_upcoming_event_artifact(
        root=tmp_path,
        relative_directory="events",
        events=json_events,
        evaluated_at_utc=CUTOFF,
    )
    loaded, rows = load_upcoming_event_artifact(
        root=tmp_path,
        relative_directory="events",
        expected_checksum=artifact.checksum_sha256,
        expected_artifact_id=artifact.artifact_id,
    )
    assert loaded.artifact_id == artifact.artifact_id
    assert rows == json_events


def test_duplicate_is_idempotent_but_contradictory_schedule_is_rejected() -> None:
    document = json.loads(upcoming_event_json_template())
    document["events"].append(dict(document["events"][0]))
    assert (
        len(
            parse_upcoming_event_json(
                dumps_canonical_json(document).encode(), evaluated_at_utc=CUTOFF
            )
        )
        == 1
    )
    document["events"][1]["event_start_utc"] = "2026-08-16T19:00:00.000000Z"
    with pytest.raises(ConfigurationError, match="contradictory"):
        parse_upcoming_event_json(dumps_canonical_json(document).encode(), evaluated_at_utc=CUTOFF)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_start_utc", "2026-07-31T19:00:00.000000Z", "start after"),
        ("event_status", "finished", "scheduled"),
        ("canonical_home_participant_id", "", "non-empty"),
        ("canonical_home_participant_id", "unresolved-team", "unresolved"),
        ("canonical_away_participant_id", "11111111-1111-5111-8111-111111111111", "differ"),
        ("competition_id", "unknown-league", "not registered"),
        ("observed_at_utc", "2026-08-01T12:00:00+00:00", "ending in Z"),
        ("operator_note", "https://example.test/request", "prohibited"),
        ("operator_note", "Authorization: secret", "prohibited"),
    ],
)
def test_invalid_event_inputs_are_rejected(field: str, value: object, message: str) -> None:
    document = json.loads(upcoming_event_json_template())
    document["events"][0][field] = value
    with pytest.raises(ConfigurationError, match=message):
        parse_upcoming_event_json(dumps_canonical_json(document).encode(), evaluated_at_utc=CUTOFF)


def test_unexpected_field_and_checksum_tampering_are_rejected(tmp_path) -> None:
    document = json.loads(upcoming_event_json_template())
    document["events"][0]["url"] = "redacted"
    with pytest.raises(ConfigurationError, match="fields"):
        parse_upcoming_event_json(dumps_canonical_json(document).encode(), evaluated_at_utc=CUTOFF)
    events = parse_upcoming_event_json(
        upcoming_event_json_template().encode(), evaluated_at_utc=CUTOFF
    )
    write_upcoming_event_artifact(
        root=tmp_path,
        relative_directory="events",
        events=events,
        evaluated_at_utc=CUTOFF,
    )
    manifest = tmp_path / "events" / "manifest.json"
    manifest.write_text(manifest.read_text().replace("round-1", "round-2"), encoding="utf-8")
    with pytest.raises(ArtifactError, match="checksum"):
        load_upcoming_event_artifact(root=tmp_path, relative_directory="events")


def test_semantic_tampering_after_reidentity_and_rechecksum_is_rejected(tmp_path) -> None:
    events = parse_upcoming_event_json(
        upcoming_event_json_template().encode(), evaluated_at_utc=CUTOFF
    )
    artifact = write_upcoming_event_artifact(
        root=tmp_path,
        relative_directory="events",
        events=events,
        evaluated_at_utc=CUTOFF,
    )
    assert isinstance(artifact.payload, dict)
    payload = dict(artifact.payload)
    rows = [dict(row) for row in payload["events"]]
    rows[0]["canonical_event_id"] = "00000000-0000-5000-8000-000000000000"
    payload["events"] = rows
    document = build_analytical_artifact_document(
        artifact_type=UPCOMING_EVENT_ARTIFACT_TYPE,
        schema_version=UPCOMING_EVENT_ARTIFACT_SCHEMA,
        payload=payload,
    )
    text = dumps_canonical_json(document) + "\n"
    (tmp_path / "events" / "manifest.json").write_text(text, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(text.encode()).hexdigest()
    (tmp_path / "events" / "manifest_checksum.sha256").write_text(
        digest + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(ArtifactError):
        load_upcoming_event_artifact(root=tmp_path, relative_directory="events")


def test_all_upcoming_event_operator_cli_modes(tmp_path, monkeypatch, capsys) -> None:
    assert engine_cli.main(["--export-upcoming-event-csv-template"]) == 0
    assert "sport_code,competition_id" in capsys.readouterr().out
    assert engine_cli.main(["--export-upcoming-event-json-template"]) == 0
    assert "operator-upcoming-events-import-v1" in capsys.readouterr().out

    source = tmp_path / "events.json"
    source.write_text(upcoming_event_json_template(), encoding="utf-8")
    cutoff = "2026-08-01T12:00:00.000000Z"
    assert (
        engine_cli.main(
            [
                "--validate-upcoming-event-input",
                str(source),
                "--as-of-utc",
                cutoff,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "valid"

    exports = tmp_path / "exports"
    runtime = SimpleNamespace(paths=SimpleNamespace(exports_directory=exports))
    monkeypatch.setattr(engine_cli, "bootstrap_runtime", lambda *_args, **_kwargs: runtime)
    assert (
        engine_cli.main(
            [
                "--import-upcoming-events",
                str(source),
                "--as-of-utc",
                cutoff,
                "--output-relative",
                "operator/events",
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert imported["state"] == "imported"
    assert (
        engine_cli.main(
            [
                "--verify-upcoming-event-artifact",
                "operator/events",
                "--checksum",
                imported["checksum_sha256"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "verified"
    assert engine_cli.main(["--list-upcoming-events", "operator/events"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["row_count"] == 1
    assert listed["events"][0]["event_status"] == "scheduled"

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from sports_analytics.artifacts import build_analytical_artifact_document
from sports_analytics.core.exceptions import ArtifactError, ConfigurationError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.sports.football.participant_registry import (
    PARTICIPANT_REGISTRY_ARTIFACT_TYPE,
    PARTICIPANT_REGISTRY_SCHEMA,
    load_participant_registry_artifact,
    parse_participant_registry_json,
    participant_registry_json_template,
    write_participant_registry_artifact,
)


def _document():
    document = json.loads(participant_registry_json_template())
    second = dict(document["participants"][0])
    second["canonical_participant_id"] = "22222222-2222-5222-8222-222222222222"
    second["canonical_display_name"] = "Second Team"
    second["source_participant_id"] = "verified-team-2"
    document["participants"].append(second)
    return document


def _parsed(document=None):
    return parse_participant_registry_json(dumps_canonical_json(document or _document()).encode())


def test_registry_template_build_reload_and_lookup(tmp_path) -> None:
    revision, generated, evaluated, participants = _parsed()
    artifact = write_participant_registry_artifact(
        root=tmp_path,
        relative_directory="registry",
        registry_revision=revision,
        generated_at_utc=generated,
        evaluated_at_utc=evaluated,
        participants=participants,
    )
    registry = load_participant_registry_artifact(
        root=tmp_path,
        relative_directory="registry",
        expected_artifact_id=artifact.artifact_id,
        expected_checksum=artifact.checksum_sha256,
    )
    assert registry.participant(participants[0].canonical_participant_id) == participants[0]
    assert len(registry.participants_for_competition("prt-primeira-liga")) == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sport_code", "tennis", "football"),
        ("participant_kind", "player", "team"),
        ("competition_ids", ["unknown-league"], "not registered"),
        ("reconciliation_state", "unresolved", "downstream safe"),
        ("reconciliation_state", "probable", "downstream safe"),
        ("source_lineage_checksum_sha256", "bad", "checksum"),
        ("valid_until", "2026-01-01", "validity interval"),
    ],
)
def test_registry_rejects_invalid_identity_evidence(field, value, message) -> None:
    document = _document()
    document["participants"][0][field] = value
    with pytest.raises(ConfigurationError, match=message):
        _parsed(document)


def test_registry_rejects_duplicate_and_contradictory_source_identity() -> None:
    document = _document()
    document["participants"][1]["canonical_participant_id"] = document["participants"][0][
        "canonical_participant_id"
    ]
    with pytest.raises(ConfigurationError, match="duplicate"):
        _parsed(document)
    document = _document()
    document["participants"][1]["source_participant_id"] = document["participants"][0][
        "source_participant_id"
    ]
    with pytest.raises(ConfigurationError, match="contradictory"):
        _parsed(document)


def test_registry_rejects_checksum_semantic_tamper_and_unexpected_file(tmp_path) -> None:
    revision, generated, evaluated, participants = _parsed()
    artifact = write_participant_registry_artifact(
        root=tmp_path,
        relative_directory="registry",
        registry_revision=revision,
        generated_at_utc=generated,
        evaluated_at_utc=evaluated,
        participants=participants,
    )
    manifest = tmp_path / "registry" / "manifest.json"
    manifest.write_text(manifest.read_text().replace("Second Team", "Tampered Team"))
    with pytest.raises(ArtifactError, match="checksum"):
        load_participant_registry_artifact(root=tmp_path, relative_directory="registry")

    # Rebuild the content identity and checksum: semantic validation must still fail.
    payload = dict(artifact.payload)
    rows = [dict(item) for item in payload["participants"]]
    rows[0]["reconciliation_state"] = "probable"
    payload["participants"] = rows
    document = build_analytical_artifact_document(
        artifact_type=PARTICIPANT_REGISTRY_ARTIFACT_TYPE,
        schema_version=PARTICIPANT_REGISTRY_SCHEMA,
        payload=payload,
    )
    text = dumps_canonical_json(document) + "\n"
    manifest.write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode()).hexdigest()
    (tmp_path / "registry" / "manifest_checksum.sha256").write_text(checksum + "\n")
    with pytest.raises(ArtifactError, match="downstream safe"):
        load_participant_registry_artifact(root=tmp_path, relative_directory="registry")

    # Restore through a second directory to exercise strict exact-file validation.
    write_participant_registry_artifact(
        root=tmp_path,
        relative_directory="registry-extra",
        registry_revision=revision,
        generated_at_utc=datetime(2026, 8, 1, 12, tzinfo=UTC),
        evaluated_at_utc=evaluated,
        participants=participants,
    )
    (tmp_path / "registry-extra" / "unexpected.txt").write_text("unexpected")
    with pytest.raises(ArtifactError, match="files"):
        load_participant_registry_artifact(root=tmp_path, relative_directory="registry-extra")

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pyarrow as pa
import pytest

from sports_analytics.core.exceptions import (
    ArtifactError,
    ConfigurationError,
    SnapshotVerificationError,
)
from sports_analytics.snapshots.writer import prepare_snapshot_directory
from sports_analytics.sports.football.participant_registry import (
    PARTICIPANT_REGISTRY_INPUT_SCHEMA,
    PARTICIPANT_SOURCE_ROLE,
    ParticipantSourceReference,
    derive_participant_registry_artifact,
    load_participant_registry_artifact,
    parse_participant_registry_json,
    participant_registry_json_template,
)
from tests.helpers_snapshots import (
    build_spec,
    build_tables,
    build_verified_participant_registry,
    database_path,
    publication_service,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _published_reference(tmp_path, *, mutation=None) -> ParticipantSourceReference:
    content = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        b"P1,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
    )
    spec, bundle = build_spec(
        tmp_path,
        competition_id="prt-primeira-liga",
        content=content,
        raw_subdirectory=f"raw-{uuid.uuid4()}",
    )
    tables = build_tables(bundle)
    if mutation is not None:
        dataset, transform = mutation
        rows = tables[dataset].to_pylist()
        transform(rows)
        tables[dataset] = pa.Table.from_pylist(rows, schema=tables[dataset].schema)
    snapshot_id = str(uuid.uuid4())
    prepared = prepare_snapshot_directory(
        snapshots_directory=tmp_path,
        spec=spec,
        tables=tables,
        snapshot_id=snapshot_id,
    )
    published = publication_service(database_path(tmp_path), tmp_path).publish_or_reuse(
        prepared, actor="test"
    )
    relative = published.snapshot_relative_path.rsplit("/", 1)[0]
    return ParticipantSourceReference(
        PARTICIPANT_SOURCE_ROLE,
        relative,
        published.snapshot_id,
        published.manifest_checksum_sha256,
        published.snapshot_type,
        published.schema_version,
    )


def _derive(tmp_path, reference, relative="registry"):
    return derive_participant_registry_artifact(
        root=tmp_path,
        source_root=tmp_path,
        relative_directory=relative,
        registry_revision="registry-1",
        evaluated_at_utc=NOW,
        source_artifacts=(reference,),
    )


def test_reference_only_template_rejects_self_attested_participant_rows() -> None:
    document = json.loads(participant_registry_json_template())
    assert document["schema_version"] == PARTICIPANT_REGISTRY_INPUT_SCHEMA
    assert set(document) == {
        "schema_version",
        "registry_revision",
        "evaluated_at_utc",
        "source_artifacts",
    }
    document["participants"] = [{"canonical_participant_id": str(uuid.uuid4())}]
    with pytest.raises(ConfigurationError, match="fields are not exact"):
        parse_participant_registry_json(json.dumps(document).encode())


def test_valid_registry_is_derived_and_reverified_from_typed_snapshot(tmp_path) -> None:
    reference = _published_reference(tmp_path)
    artifact = _derive(tmp_path, reference)
    registry = load_participant_registry_artifact(
        root=tmp_path,
        source_root=tmp_path,
        relative_directory="registry",
        expected_artifact_id=artifact.artifact_id,
        expected_checksum=artifact.checksum_sha256,
    )
    assert len(registry.participants) == 2
    assert {item.participant_kind for item in registry.participants} == {"club"}
    assert {item.competition_ids for item in registry.participants} == {("prt-primeira-liga",)}
    assert registry.source_artifacts == (reference,)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("checksum_sha256", "0" * 64, "checksum"),
        ("artifact_id", str(uuid.uuid4()), "identity"),
        ("artifact_type", "generic-artifact", "unsupported"),
        ("schema_version", "self-declared-v1", "unsupported"),
        ("role", "operator-participant-rows", "unsupported"),
    ],
)
def test_source_reference_must_match_allowlisted_loaded_artifact(
    tmp_path, field, value, message
) -> None:
    reference = replace(_published_reference(tmp_path), **{field: value})
    with pytest.raises((ConfigurationError, SnapshotVerificationError), match=message):
        _derive(tmp_path, reference)


def test_missing_source_and_post_publication_tampering_fail_closed(tmp_path) -> None:
    reference = _published_reference(tmp_path)
    artifact = _derive(tmp_path, reference)
    missing = replace(reference, relative_directory="missing/source")
    with pytest.raises(SnapshotVerificationError, match="missing"):
        _derive(tmp_path, missing, "missing-registry")
    participant_file = tmp_path / reference.relative_directory / "participants.parquet"
    participant_file.write_bytes(participant_file.read_bytes() + b"tamper")
    with pytest.raises(ArtifactError, match="checksum"):
        load_participant_registry_artifact(
            root=tmp_path,
            source_root=tmp_path,
            relative_directory=artifact.relative_directory,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            (
                "participant_reconciliations",
                lambda rows: rows[0].update(
                    {
                        "reconciliation_state": "unresolved",
                        "canonical_participant_id": None,
                        "reconciliation_confidence": 0.0,
                        "reason": "ambiguous",
                    }
                ),
            ),
            "mismatch|unresolved",
        ),
        (
            (
                "participants",
                lambda rows: rows[0].update({"sport_code": "tennis"}),
            ),
            "sport",
        ),
        (
            (
                "participants",
                lambda rows: rows[0].update({"participant_type": "player"}),
            ),
            "kind",
        ),
        (
            (
                "source_participants",
                lambda rows: rows[0].update({"competition_id": "unknown-league"}),
            ),
            "contradictory",
        ),
        (
            (
                "participant_reconciliations",
                lambda rows: rows[0].update(
                    {"canonical_participant_id": rows[1]["canonical_participant_id"]}
                ),
            ),
            "mismatch",
        ),
        (
            (
                "participant_reconciliations",
                lambda rows: rows[0].update({"source_participant_id": str(uuid.uuid4())}),
            ),
            "absent",
        ),
    ],
)
def test_semantically_invalid_typed_snapshot_is_rejected(tmp_path, mutation, message) -> None:
    reference = _published_reference(tmp_path, mutation=mutation)
    with pytest.raises(ConfigurationError, match=message):
        _derive(tmp_path, reference)


def test_fixture_supports_registered_model_unseen_identity(tmp_path) -> None:
    home = "11111111-1111-5111-8111-111111111111"
    away = "22222222-2222-5222-8222-222222222222"
    _, registry, _ = build_verified_participant_registry(
        tmp_path,
        root=tmp_path,
        canonical_participant_ids=(home, away),
        relative_directory="registry",
        evaluated_at_utc=NOW,
    )
    assert (
        registry.require_registered_participant(
            away,
            competition_id="prt-primeira-liga",
            event_date=NOW.date(),
        ).canonical_participant_id
        == away
    )

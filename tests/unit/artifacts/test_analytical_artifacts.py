"""Strict analytical artifact identity and filesystem integrity tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sports_analytics.artifacts import (
    load_analytical_artifact,
    load_typed_analytical_artifact,
    write_analytical_artifact,
    write_typed_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.services.engine_cli import main as engine_main


def _write(root: Path, relative: str = "backtests/example/id-1"):
    return write_analytical_artifact(
        root=root,
        relative_directory=relative,
        artifact_type="backtest",
        schema_version="generic-backtest-v1",
        payload={"metrics": {"roi": 0.1}, "bet_ids": ["a", "b"]},
    )


def _typed_datasets():
    prediction = {
        "prediction_id": "prediction-1",
        "ordered_selection_ids": ["selection-a", "selection-b", "selection-c"],
        "probabilities": [
            {"selection_id": "selection-a", "probability": 0.5},
            {"selection_id": "selection-b", "probability": 0.3},
            {"selection_id": "selection-c", "probability": 0.2},
        ],
    }
    opportunity = {
        "opportunity_id": "opportunity-1",
        "event_start_utc": "2024-02-10T15:00:00Z",
        "decision_as_of_utc": "2024-02-10T14:00:00Z",
        "expected_value": 0.2,
        "model_artifact_id": "model-1",
        "model_checksum_sha256": "a" * 64,
        "model_specification_version": "model-v1",
        "feature_artifact_id": "feature-1",
        "feature_manifest_checksum_sha256": "b" * 64,
        "feature_specification_version": "feature-v1",
        "feature_row_id": "event-1",
    }
    return {
        "predictions": (prediction,),
        "market_evaluations": ({"evaluation_id": "evaluation-1", "expected_value": 0.2},),
        "opportunity_decisions": ({"opportunity_id": "opportunity-1", "eligible": True},),
        "opportunities": (opportunity,),
        "combinations": (),
        "rejections": (),
    }


def test_artifact_is_atomic_content_addressed_and_deterministic(tmp_path: Path) -> None:
    first = _write(tmp_path / "one")
    second = _write(tmp_path / "two")
    assert first.artifact_id == second.artifact_id
    assert first.checksum_sha256 == second.checksum_sha256
    loaded = load_analytical_artifact(
        root=tmp_path / "one",
        relative_directory=first.relative_directory,
        expected_artifact_type="backtest",
        expected_schema_version="generic-backtest-v1",
        expected_checksum=first.checksum_sha256,
        expected_artifact_id=first.artifact_id,
    )
    assert loaded == first
    with pytest.raises(ArtifactError, match="already exists"):
        _write(tmp_path / "one")


def test_artifact_rejects_extras_and_symlinks(tmp_path: Path) -> None:
    artifact = _write(tmp_path)
    directory = tmp_path / artifact.relative_directory
    (directory / "extra.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="missing or extra"):
        load_analytical_artifact(
            root=tmp_path,
            relative_directory=artifact.relative_directory,
            expected_artifact_type="backtest",
            expected_schema_version="generic-backtest-v1",
        )
    (directory / "extra.json").unlink()
    checksum = directory / "manifest_checksum.sha256"
    real = tmp_path / "real.sha256"
    checksum.rename(real)
    checksum.symlink_to(real)
    with pytest.raises(ArtifactError, match="symlink"):
        load_analytical_artifact(
            root=tmp_path,
            relative_directory=artifact.relative_directory,
            expected_artifact_type="backtest",
            expected_schema_version="generic-backtest-v1",
        )


def test_artifact_rejects_rechecksummed_payload_with_stale_id(tmp_path: Path) -> None:
    artifact = _write(tmp_path)
    directory = tmp_path / artifact.relative_directory
    manifest = directory / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["payload"]["metrics"]["roi"] = 9.9
    text = dumps_canonical_json(document) + "\n"
    manifest.write_text(text, encoding="utf-8", newline="\n")
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (directory / "manifest_checksum.sha256").write_text(
        f"{checksum}\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ArtifactError, match="id does not match"):
        load_analytical_artifact(
            root=tmp_path,
            relative_directory=artifact.relative_directory,
            expected_artifact_type="backtest",
            expected_schema_version="generic-backtest-v1",
        )


def test_artifact_rejects_path_traversal_and_wrong_schema(tmp_path: Path) -> None:
    artifact = _write(tmp_path)
    with pytest.raises(ArtifactError):
        load_analytical_artifact(
            root=tmp_path,
            relative_directory="../escape",
            expected_artifact_type="backtest",
            expected_schema_version="generic-backtest-v1",
        )
    with pytest.raises(ArtifactError, match="schema version"):
        load_analytical_artifact(
            root=tmp_path,
            relative_directory=artifact.relative_directory,
            expected_artifact_type="backtest",
            expected_schema_version="generic-backtest-v2",
        )


def test_engine_cli_strictly_verifies_analysis_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exports = tmp_path / "exports"
    artifact = _write(exports)
    config = tmp_path / "settings.toml"
    config.write_text(
        "\n".join(
            (
                "[storage]",
                f'root_directory = "{(tmp_path / "storage").as_posix()}"',
                f'exports_directory = "{exports.as_posix()}"',
                f'sqlite_path = "{(tmp_path / "operational.sqlite3").as_posix()}"',
                "[logging]",
                "file_enabled = false",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    code = engine_main(
        [
            "--config",
            str(config),
            "--verify-analysis-artifact",
            artifact.relative_directory,
            "--artifact-type",
            "backtest",
            "--artifact-schema",
            "generic-backtest-v1",
            "--checksum",
            artifact.checksum_sha256,
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["artifact_id"] == artifact.artifact_id


def test_typed_artifact_declares_and_verifies_authoritative_datasets(
    tmp_path: Path,
) -> None:
    artifact = write_typed_analytical_artifact(
        root=tmp_path,
        relative_directory="analysis/id-1",
        artifact_kind="analysis",
        schema_version="analysis-v1",
        datasets=_typed_datasets(),
    )
    assert {item.name for item in artifact.datasets} == set(_typed_datasets())
    loaded = load_typed_analytical_artifact(
        root=tmp_path,
        relative_directory="analysis\\id-1",
        expected_kind="analysis",
        expected_schema_version="analysis-v1",
        expected_artifact_id=artifact.artifact_id,
    )
    assert loaded.dataset("predictions").row_count == 1
    assert loaded.dataset("opportunities").rows[0]["opportunity_id"] == "opportunity-1"


def test_typed_backtest_layout_requires_settlements_and_metric_datasets(
    tmp_path: Path,
) -> None:
    datasets = _typed_datasets()
    datasets.update(
        {
            "settlements": ({"bet_id": "bet-1", "result": "win"},),
            "fold_metrics": ({"fold_id": "fold-1", "sample_size": 1},),
            "aggregate_metrics": ({"metric_id": "aggregate", "bet_count": 1},),
        }
    )
    artifact = write_typed_analytical_artifact(
        root=tmp_path,
        relative_directory="backtests/id-1",
        artifact_kind="backtest",
        schema_version="backtest-v1",
        datasets=datasets,
    )
    assert artifact.dataset("settlements").rows[0]["result"] == "win"
    assert artifact.dataset("aggregate_metrics").row_count == 1


def test_typed_artifact_rejects_duplicate_ids_hash_tampering_and_bad_timing(
    tmp_path: Path,
) -> None:
    duplicate = _typed_datasets()
    duplicate["predictions"] = (
        duplicate["predictions"][0],
        duplicate["predictions"][0],
    )
    with pytest.raises(ArtifactError, match="duplicate"):
        write_typed_analytical_artifact(
            root=tmp_path,
            relative_directory="analysis/duplicate",
            artifact_kind="analysis",
            schema_version="analysis-v1",
            datasets=duplicate,
        )
    bad_timing = _typed_datasets()
    bad_opportunity = dict(bad_timing["opportunities"][0])
    bad_opportunity["decision_as_of_utc"] = "2024-02-10T16:00:00Z"
    bad_timing["opportunities"] = (bad_opportunity,)
    with pytest.raises(ArtifactError, match="timing follows"):
        write_typed_analytical_artifact(
            root=tmp_path,
            relative_directory="analysis/bad-timing",
            artifact_kind="analysis",
            schema_version="analysis-v1",
            datasets=bad_timing,
        )
    artifact = write_typed_analytical_artifact(
        root=tmp_path,
        relative_directory="analysis/tamper",
        artifact_kind="analysis",
        schema_version="analysis-v1",
        datasets=_typed_datasets(),
    )
    directory = tmp_path / artifact.relative_directory
    opportunities = directory / "opportunities.jsonl"
    opportunities.write_text(
        opportunities.read_text(encoding="utf-8").replace(
            "2024-02-10T14:00:00Z",
            "2024-02-10T16:00:00Z",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        load_typed_analytical_artifact(
            root=tmp_path,
            relative_directory=artifact.relative_directory,
            expected_kind="analysis",
            expected_schema_version="analysis-v1",
        )


@pytest.mark.parametrize(
    "mode",
    (
        "--generate-predictions",
        "--evaluate-opportunities",
        "--build-combinations",
        "--validate-combination",
        "--run-backtest",
    ),
)
def test_focused_json_commands_report_clean_malformed_json_errors(
    mode: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    malformed = tmp_path / "bad.json"
    malformed.write_text('{"broken":', encoding="utf-8")
    assert engine_main([mode, str(malformed)]) == 2
    captured = capsys.readouterr()
    assert "JSON input is malformed" in captured.err
    assert "Traceback" not in captured.err


def test_generate_predictions_command_uses_declared_order(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = {
        "canonical_event_id": "event-1",
        "event_start_utc": "2024-02-10T15:00:00Z",
        "predicted_at_utc": "2024-02-10T12:00:00Z",
        "feature_available_at_utc": "2024-02-10T11:00:00Z",
        "lineage": {
            "model_artifact_id": "model-1",
            "model_checksum_sha256": "a" * 64,
            "model_specification_version": "model-v1",
            "feature_artifact_id": "feature-1",
            "feature_manifest_checksum_sha256": "b" * 64,
            "feature_specification_version": "feature-v1",
            "feature_row_id": "event-1",
            "trained_through_date": "2024-02-01",
            "calibrated_through_date": "2024-02-02",
        },
        "probabilities": [
            {
                "sport_code": "tennis",
                "market_family": "winner",
                "market_key": "tennis.match-winner.full-match",
                "market_period": "full-match",
                "participant_scope": "event",
                "canonical_participant_id": None,
                "line_type": "none",
                "line_value": None,
                "outcome_key": outcome,
                "probability": probability,
            }
            for outcome, probability in (("player-z", 0.6), ("player-a", 0.4))
        ],
    }
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    assert engine_main(["--generate-predictions", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [item["probability"] for item in output["probabilities"]] == [0.6, 0.4]
    assert output["production_eligible"] is True

"""Focused local v1 initialization, doctor, recovery, and packaging tests."""

from __future__ import annotations

import json
import sqlite3
import sys
import tomllib
from contextlib import closing
from pathlib import Path

import pytest

import sports_analytics
import sports_analytics.release.backup as backup_module
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.local.supervisor import LocalSupervisor
from sports_analytics.release.backup import (
    BACKUP_FORMAT,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from sports_analytics.release.cli import initialize_v1, main
from sports_analytics.release.doctor import inspect_release_readiness


def _write_config(base: Path, root_name: str = "runtime") -> Path:
    config = base / f"{root_name}.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        f'[storage]\nroot_directory = "{root_name}"\n'
        f'sqlite_path = "{root_name}/operational.sqlite3"\n'
        f'raw_directory = "{root_name}/raw"\n'
        f'snapshots_directory = "{root_name}/snapshots"\n'
        f'features_directory = "{root_name}/features"\n'
        f'models_directory = "{root_name}/models"\n'
        f'exports_directory = "{root_name}/exports"\n'
        f'logs_directory = "{root_name}/logs"\n',
        encoding="utf-8",
    )
    return config


def _paths(base: Path, config: Path):
    settings = load_settings(config_path=config, environ={}, base_directory=base)
    return resolve_paths(settings, base)


def test_initialize_is_idempotent_and_creates_only_runtime_state(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    original = config.read_bytes()

    first = initialize_v1(config_path=config, base_directory=tmp_path)
    second = initialize_v1(config_path=config, base_directory=tmp_path)

    assert first["application_version"] == "1.0.0"
    assert first["database_migration_state"]["applied_now"] == [1, 2, 3, 4, 5]
    assert second["database_migration_state"]["applied_now"] == []
    assert second["database_migration_state"]["current_version"] == 5
    assert first["bookmakers_enabled"] is False
    assert first["supported_current_price_path"] == "strict-offline-operator-input"
    assert first["placement_mode"] == "manual-only"
    assert config.read_bytes() == original
    paths = _paths(tmp_path, config)
    assert not any(paths.models_directory.iterdir())
    assert not any(paths.exports_directory.iterdir())
    assert not (tmp_path / ".env").exists()


def test_doctor_missing_database_is_read_only(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    first = inspect_release_readiness(config_path=config, base_directory=tmp_path)
    second = inspect_release_readiness(config_path=config, base_directory=tmp_path)

    assert first == second
    assert first["overall_state"] == "not-initialized"
    assert first["manual_placement_only"] is True
    assert first["bookmaker_network_required_for_v1"] is False
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_doctor_initialized_empty_runtime_is_degraded_not_invalid(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    initialize_v1(config_path=config, base_directory=tmp_path)

    report = inspect_release_readiness(config_path=config, base_directory=tmp_path)

    assert report["overall_state"] == "degraded"
    assert report["blockers"] == []
    assert report["checks"]["sqlite"]["integrity"] == "ok"
    assert report["checks"]["migration"]["current_version"] == 5
    assert report["checks"]["latest_product_state"]["state"] == "optional-data-absent"


def test_doctor_rejects_corrupt_database_without_disclosing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    paths = _paths(tmp_path, config)
    paths.storage_root.mkdir()
    paths.sqlite_path.write_text("not sqlite", encoding="utf-8")
    monkeypatch.setenv("SPORTS_ANALYTICS_FAKE_SECRET", "never-print-this")

    report = inspect_release_readiness(config_path=config, base_directory=tmp_path)
    encoded = json.dumps(report, sort_keys=True)

    assert report["overall_state"] == "invalid"
    assert "never-print-this" not in encoded


def test_doctor_rejects_migration_history_mismatch(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    initialize_v1(config_path=config, base_directory=tmp_path)
    paths = _paths(tmp_path, config)
    with closing(sqlite3.connect(paths.sqlite_path)) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 5",
            ("0" * 64,),
        )
        connection.commit()

    report = inspect_release_readiness(config_path=config, base_directory=tmp_path)

    assert report["overall_state"] == "invalid"
    assert any("corrupt or incompatible" in blocker for blocker in report["blockers"])


def test_backup_and_restore_round_trip_with_content_manifest(tmp_path: Path) -> None:
    source_config = _write_config(tmp_path, "source")
    initialize_v1(config_path=source_config, base_directory=tmp_path)
    source_paths = _paths(tmp_path, source_config)
    (source_paths.raw_directory / "capture.json").write_text('{"ok":true}\n', encoding="utf-8")
    (source_paths.models_directory / "model.bin").write_bytes(b"immutable-model")
    (source_paths.logs_directory / "ignored.log").write_text("ignored", encoding="utf-8")
    (source_paths.storage_root / ".env").write_text("SECRET=ignored", encoding="utf-8")

    backup = tmp_path / "backup-v1"
    result = create_backup(backup, paths=source_paths, explicit_config=source_config)
    manifest = verify_backup(backup)

    assert result["format"] == BACKUP_FORMAT
    assert manifest["format"] == BACKUP_FORMAT
    inventory = {item["relative_path"] for item in manifest["files"]}
    assert "state/raw/capture.json" in inventory
    assert "state/models/model.bin" in inventory
    assert all(".env" not in item and "logs/" not in item for item in inventory)

    restore_config = _write_config(tmp_path, "restored")
    restored_paths = _paths(tmp_path, restore_config)
    restored = restore_backup(backup, paths=restored_paths)

    assert restored["state"] == "restore-complete"
    assert (restored_paths.raw_directory / "capture.json").read_bytes() == (
        source_paths.raw_directory / "capture.json"
    ).read_bytes()
    assert (restored_paths.models_directory / "model.bin").read_bytes() == b"immutable-model"
    assert restored_paths.sqlite_path.is_file()
    report = inspect_release_readiness(
        config_path=restore_config,
        base_directory=tmp_path,
    )
    assert report["overall_state"] == "degraded"


@pytest.mark.parametrize("mutation", ["unexpected", "missing", "altered"])
def test_backup_verification_rejects_file_set_and_content_tampering(
    tmp_path: Path, mutation: str
) -> None:
    config = _write_config(tmp_path)
    initialize_v1(config_path=config, base_directory=tmp_path)
    paths = _paths(tmp_path, config)
    fixture = paths.raw_directory / "fixture.txt"
    fixture.write_text("trusted", encoding="utf-8")
    backup = tmp_path / "backup"
    create_backup(backup, paths=paths)
    copied = backup / "state" / "raw" / "fixture.txt"
    if mutation == "unexpected":
        (backup / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    elif mutation == "missing":
        copied.unlink()
    else:
        copied.write_text("altered", encoding="utf-8")

    with pytest.raises(BackupError):
        verify_backup(backup)


def test_restore_rejects_existing_database_and_nonempty_destination(tmp_path: Path) -> None:
    source_config = _write_config(tmp_path, "source")
    initialize_v1(config_path=source_config, base_directory=tmp_path)
    backup = tmp_path / "backup"
    create_backup(backup, paths=_paths(tmp_path, source_config))

    restore_config = _write_config(tmp_path, "restore")
    restore_paths = _paths(tmp_path, restore_config)
    restore_paths.raw_directory.mkdir(parents=True)
    (restore_paths.raw_directory / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(backup, paths=restore_paths)
    assert (restore_paths.raw_directory / "existing.txt").read_text(encoding="utf-8") == "keep"
    assert not restore_paths.sqlite_path.exists()


def test_backup_rejects_existing_destination_and_source_symlink_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    initialize_v1(config_path=config, base_directory=tmp_path)
    paths = _paths(tmp_path, config)
    existing = tmp_path / "existing-backup"
    existing.mkdir()
    with pytest.raises(BackupError, match="new and non-existing"):
        create_backup(existing, paths=paths)

    source = paths.raw_directory / "reported-symlink"
    source.write_text("fixture", encoding="utf-8")
    original = Path.is_symlink

    def reported_symlink(path: Path) -> bool:
        return path == source or original(path)

    monkeypatch.setattr(Path, "is_symlink", reported_symlink)
    with pytest.raises(BackupError, match="symlink"):
        create_backup(tmp_path / "new-backup", paths=paths)


def test_recomputed_forged_manifest_with_changed_semantics_is_rejected(
    tmp_path: Path,
) -> None:
    config = _write_config(tmp_path)
    initialize_v1(config_path=config, base_directory=tmp_path)
    backup = tmp_path / "backup"
    create_backup(backup, paths=_paths(tmp_path, config))
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_directories"]["raw"] = "state/models"
    manifest_path.write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BackupError, match="roles"):
        verify_backup(backup)


def test_interrupted_restore_rolls_back_newly_published_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config = _write_config(tmp_path, "source")
    initialize_v1(config_path=source_config, base_directory=tmp_path)
    source_paths = _paths(tmp_path, source_config)
    (source_paths.raw_directory / "fixture.txt").write_text("trusted", encoding="utf-8")
    backup = tmp_path / "backup"
    create_backup(backup, paths=source_paths)

    restore_config = _write_config(tmp_path, "restore")
    restore_paths = _paths(tmp_path, restore_config)
    real_rename = backup_module.os.rename
    calls = 0

    def fail_second_publish(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated publication interruption")
        real_rename(source, destination)

    monkeypatch.setattr(backup_module.os, "rename", fail_second_publish)
    with pytest.raises(BackupError, match="without retained partial state"):
        restore_backup(backup, paths=restore_paths)

    assert not restore_paths.sqlite_path.exists()
    assert not restore_paths.raw_directory.exists()
    assert not restore_paths.snapshots_directory.exists()


def test_console_version_and_release_package_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "1.0.0"
    assert sports_analytics.__version__ == "1.0.0"
    assert metadata["project"]["version"] == "1.0.0"
    assert (
        metadata["project"]["scripts"]["sports-analytics-v1"] == "sports_analytics.release.cli:main"
    )
    assert Path("src/sports_analytics/ui/streamlit_entry.py").is_file()


def test_supervisor_builds_exact_loopback_package_commands() -> None:
    runner = LocalSupervisor(install_signals=False)

    worker = runner._build_worker_command(
        config=None,
        env_file=None,
        worker_once=False,
        worker_max_jobs=None,
        worker_id=None,
    )
    ui = runner._build_ui_command(config=None, env_file=None, ui_port=9123)

    assert worker[1:] == ["-m", "sports_analytics.jobs"]
    assert ui[:4] == [sys.executable, "-m", "streamlit", "run"]
    assert "--server.address=127.0.0.1" in ui
    assert "--server.port=9123" in ui
    assert "--server.headless=true" in ui
    assert "--browser.gatherUsageStats=false" in ui
    assert "0.0.0.0" not in " ".join(ui)
    assert all("shell" not in item for item in ui)


def test_v1_product_ui_contains_no_mutating_controls() -> None:
    source = Path("src/sports_analytics/ui/product_pages.py").read_text(encoding="utf-8")
    prohibited = (
        "form_submit_button",
        "Publish immutable policy",
        "publish_proposal_policy",
    )
    assert not any(item in source for item in prohibited)

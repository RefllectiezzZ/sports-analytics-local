from __future__ import annotations

from pathlib import Path

from sports_analytics.release import cli


def _config(base: Path) -> Path:
    path = base / "settings.toml"
    path.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nroot_directory = "runtime"\n'
        'sqlite_path = "runtime/operational.sqlite3"\n'
        'raw_directory = "runtime/raw"\n'
        'snapshots_directory = "runtime/snapshots"\n'
        'features_directory = "runtime/features"\n'
        'models_directory = "runtime/models"\n'
        'exports_directory = "runtime/exports"\n'
        'logs_directory = "runtime/logs"\n',
        encoding="utf-8",
    )
    return path


def test_normal_launch_initializes_before_worker_and_ui(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    original = config.read_bytes()
    calls: list[dict[str, object]] = []

    class Supervisor:
        def run(self, **kwargs: object) -> int:
            assert (tmp_path / "runtime" / "operational.sqlite3").is_file()
            calls.append(kwargs)
            return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "LocalSupervisor", Supervisor)

    assert cli.main(["--config", str(config)]) == 0
    assert cli.main(["--config", str(config)]) == 0
    assert len(calls) == 2
    assert all(item["start_ui"] is True for item in calls)
    assert config.read_bytes() == original

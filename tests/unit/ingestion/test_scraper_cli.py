"""Section 60: scraper.py CLI tests for football ingestion commands."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from sports_analytics.sources.catalog import FOOTBALL_DATA_ADAPTER_VERSION
from sports_analytics.sources.contracts import SourceCapability, SourceRole
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
)
from tests.helpers import repository_root, scrubbed_subprocess_environ
from tests.helpers_snapshots import SYNTHETIC_CSV_WITH_ODDS, prepare, publication_service

EXPECTED_SOURCE_LINE = (
    "football-data-co-uk\tFootball-Data.co.uk\thistorical-data\t"
    "football-data-co-uk-adapter-v1\t"
    "historical-odds,historical-results,historical-statistics\tfootball"
)
EXPECTED_BETANO_SOURCE_LINE = (
    "betano-pt\tBetano Portugal\tbookmaker\tbetano-pt-adapter-v1\t"
    "current-fixtures,current-odds\tbasketball,football,tennis"
)
EXPECTED_BETCLIC_SOURCE_LINE = (
    "betclic-pt\tBetclic Portugal\tbookmaker\tbetclic-pt-adapter-v1\t"
    "current-fixtures,current-odds\tbasketball,football,tennis"
)
EXPECTED_SOURCE_LINES = (
    EXPECTED_BETANO_SOURCE_LINE,
    EXPECTED_BETCLIC_SOURCE_LINE,
    EXPECTED_SOURCE_LINE,
)
EXPECTED_VERIFY_ROW_COUNTS = (
    "competitions=1 seasons=1 participants=2 source_participants=2 "
    "participant_reconciliations=2 events=1 source_events=1 "
    "event_reconciliations=1 market_quotes=3 post_match_statistics=1"
)


def _write_scraping_config(base: Path) -> None:
    config_dir = base / "config"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text(
        """
[logging]
file_enabled = false

[scraping]
enabled = true
maximum_retries = 0
minimum_request_interval_seconds = 0
retry_backoff_base_seconds = 0.1
retry_backoff_max_seconds = 0.1
""".lstrip(),
        encoding="utf-8",
    )


def _subprocess_env() -> dict[str, str]:
    env = scrubbed_subprocess_environ()
    src_path = str(repository_root() / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_path if not existing else src_path + os.pathsep + existing
    return env


def _run_scraper(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    scraper = repository_root() / "scraper.py"
    assert scraper.is_absolute()
    return subprocess.run(
        [sys.executable, str(scraper), *args],
        cwd=tmp_path,
        env=_subprocess_env(),
        text=True,
        capture_output=True,
        check=False,
    )


def _publish_snapshot(tmp_path: Path) -> str:
    """Publish one READY football snapshot into the CLI storage layout."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    database = storage / "operational.sqlite3"
    ensure_database_ready(database)
    snapshots_directory = storage / "snapshots"
    prepared = prepare(
        tmp_path / "build",
        snapshot_id="11111111-1111-4111-8111-111111111111",
        snapshots_directory=snapshots_directory,
        content=SYNTHETIC_CSV_WITH_ODDS,
    )
    published = publication_service(database, snapshots_directory).publish_or_reuse(
        prepared,
        actor="test",
    )
    return published.snapshot_id


def test_scraper_lists_sources_without_bootstrapping_database(tmp_path: Path) -> None:
    result = _run_scraper(tmp_path, "--list-sources")

    assert result.returncode == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines == list(EXPECTED_SOURCE_LINES)
    football_line = next(line for line in lines if line.startswith("football-data-co-uk\t"))
    source_id, display_name, role, adapter_version, capabilities, sports = football_line.split("\t")
    assert source_id == SOURCE_FOOTBALL_DATA_CO_UK
    assert display_name == "Football-Data.co.uk"
    assert role == SourceRole.HISTORICAL_DATA.value
    assert adapter_version == FOOTBALL_DATA_ADAPTER_VERSION
    assert capabilities.split(",") == [
        SourceCapability.HISTORICAL_ODDS.value,
        SourceCapability.HISTORICAL_RESULTS.value,
        SourceCapability.HISTORICAL_STATISTICS.value,
    ]
    assert sports == "football"
    assert not (tmp_path / "storage" / "operational.sqlite3").exists()


def test_scraper_list_sources_includes_bookmaker_providers(tmp_path: Path) -> None:
    result = _run_scraper(tmp_path, "--list-sources")

    assert result.returncode == 0
    lowered = result.stdout.lower()
    assert "betclic-pt" in lowered
    assert "betano-pt" in lowered
    assert SourceCapability.CURRENT_ODDS.value in lowered
    assert SourceCapability.CURRENT_FIXTURES.value in lowered
    assert SourceCapability.SETTLEMENT_RESULTS.value not in lowered


def test_scraper_lists_competitions_without_bootstrapping_database(tmp_path: Path) -> None:
    result = _run_scraper(tmp_path, "--list-competitions")

    assert result.returncode == 0
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert "eng-premier-league\tPremier League\tE0\tEurope/London" in lines
    assert "prt-primeira-liga\tPrimeira Liga\tP1\tEurope/Lisbon" in lines
    assert not (tmp_path / "storage" / "operational.sqlite3").exists()


def test_scraper_enqueue_football_data_creates_pending_job(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    raw_sha = "a" * 64

    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "eng-premier-league",
        "--season",
        "2023-2024",
        "--raw-sha256",
        raw_sha,
        "--priority",
        "7",
        "--maximum-attempts",
        "2",
    )

    assert result.returncode == 0
    assert "error:" not in result.stderr.lower()
    assert "runtime bootstrap complete" in result.stderr
    assert "enqueued job_id=" in result.stdout
    assert "competition=eng-premier-league" in result.stdout
    assert "season=2023-2024" in result.stdout
    database_path = tmp_path / "storage" / "operational.sqlite3"
    with connect_database(database_path, read_only=True) as connection:
        jobs = JobRepository(connection).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_type == INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
    assert jobs[0].status is JobStatus.PENDING
    assert jobs[0].priority == 7
    assert jobs[0].maximum_attempts == 2
    assert jobs[0].payload == {
        "competition_id": "eng-premier-league",
        "season": "2023-2024",
        "raw_sha256": raw_sha,
    }


def test_scraper_enqueue_requires_competition_and_season(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)

    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "eng-premier-league",
    )

    assert result.returncode == 2
    assert "--enqueue-football-data requires --competition and --season" in result.stderr


def test_scraper_enqueue_requires_scraping_enabled(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text(
        "[logging]\nfile_enabled = false\n[scraping]\nenabled = false\n",
        encoding="utf-8",
    )
    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "eng-premier-league",
        "--season",
        "2023-2024",
    )
    assert result.returncode == 2
    assert "scraping.enabled" in result.stderr
    assert "Traceback" not in result.stderr


def test_scraper_verify_snapshot_reports_dataset_row_counts(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    snapshot_id = _publish_snapshot(tmp_path)

    result = _run_scraper(tmp_path, "--verify-snapshot", snapshot_id)

    assert result.returncode == 0
    assert "error:" not in result.stderr.lower()
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    prefix = (
        f"verified snapshot_id={snapshot_id} "
        f"type={FOOTBALL_INGESTION_SNAPSHOT_TYPE} "
        f"schema={FOOTBALL_CANONICAL_SCHEMA_VERSION} "
        f"files=10 rows[{EXPECTED_VERIFY_ROW_COUNTS}] manifest_sha256="
    )
    assert lines[0].startswith(prefix)
    assert re.fullmatch(r"[0-9a-f]{64}", lines[0].removeprefix(prefix)) is not None


def test_scraper_verify_snapshot_rejects_unknown_snapshot(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    _publish_snapshot(tmp_path)

    result = _run_scraper(tmp_path, "--verify-snapshot", "22222222-2222-4222-8222-222222222222")

    assert result.returncode == 2
    assert "snapshot not found" in result.stderr.lower()
    assert "Traceback" not in result.stderr


def test_scraper_verify_snapshot_rejects_invalid_snapshot_id(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    _publish_snapshot(tmp_path)

    result = _run_scraper(tmp_path, "--verify-snapshot", "not-a-uuid")

    assert result.returncode == 2
    assert "invalid snapshot id" in result.stderr.lower()
    assert "Traceback" not in result.stderr


def test_scraper_list_snapshots_missing_database_returns_two(tmp_path: Path) -> None:
    result = _run_scraper(tmp_path, "--list-snapshots")
    assert result.returncode == 2
    assert "error:" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "storage").exists()


def test_scraper_incompatible_modes_rejected(tmp_path: Path) -> None:
    result = _run_scraper(tmp_path, "--list-sources", "--list-competitions")
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr.lower() or "error" in result.stderr.lower()


def test_scraper_validate_config_still_works(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[application]\nenvironment = "test"\n', encoding="utf-8")
    result = _run_scraper(tmp_path, "--config", str(config), "--validate-config")
    assert result.returncode == 0
    assert "configuration valid" in result.stdout
    assert not (tmp_path / "storage").exists()


def test_scraper_smoke_unknown_telemetry_is_consistently_null(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        "[bookmakers]\nenabled = true\n",
        encoding="utf-8",
    )
    diagnostics = tmp_path / "diagnostics"
    result = _run_scraper(
        tmp_path,
        "--config",
        str(config),
        "--smoke-bookmaker",
        "--provider",
        "betclic-pt",
        "--sport",
        "football",
        "--duration-seconds",
        "5",
        "--diagnostic-directory",
        str(diagnostics),
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["failure_reason"] == "no-verified-extraction-profile"
    assert payload["failure_telemetry"] is None
    assert payload["acceptance_summary"]["failure_telemetry"] is None
    persisted = json.loads(
        (diagnostics / payload["diagnostic_relative_path"]).read_text(encoding="utf-8")
    )
    assert persisted["failure_telemetry"] is None
    assert persisted["acceptance_summary"]["failure_telemetry"] is None


def test_scraper_enqueue_invalid_competition_creates_no_side_effects(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "not-a-competition",
        "--season",
        "2023-2024",
    )
    assert result.returncode == 2
    assert "error:" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "storage").exists()
    assert not list(tmp_path.glob("**/*.sqlite3"))
    assert not list(tmp_path.glob("**/*.log"))


def test_scraper_enqueue_invalid_season_creates_no_side_effects(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "eng-premier-league",
        "--season",
        "23-24",
    )
    assert result.returncode == 2
    assert "error:" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "storage").exists()


def test_scraper_enqueue_invalid_raw_sha_creates_no_side_effects(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "eng-premier-league",
        "--season",
        "2023-2024",
        "--raw-sha256",
        "not-a-sha",
    )
    assert result.returncode == 2
    assert "error:" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "storage").exists()


def test_scraper_enqueue_rejects_priority_with_plus_and_leading_zeros(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    for bad in ("+7", "07", "1.5", "1e2", " 7"):
        result = _run_scraper(
            tmp_path,
            "--enqueue-football-data",
            "--competition",
            "eng-premier-league",
            "--season",
            "2023-2024",
            "--priority",
            bad,
        )
        assert result.returncode == 2, bad
        assert "priority" in result.stderr.lower()
        assert "Traceback" not in result.stderr
        assert not (tmp_path / "storage").exists()


def test_scraper_enqueue_rejects_huge_maximum_attempts(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    result = _run_scraper(
        tmp_path,
        "--enqueue-football-data",
        "--competition",
        "eng-premier-league",
        "--season",
        "2023-2024",
        "--maximum-attempts",
        "9" * 80,
    )
    assert result.returncode == 2
    assert "maximum_attempts" in result.stderr.lower()
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "storage").exists()


def test_scraper_list_sources_validates_config_without_side_effects(tmp_path: Path) -> None:
    _write_scraping_config(tmp_path)
    result = _run_scraper(tmp_path, "--list-sources")
    assert result.returncode == 0
    assert result.stdout.splitlines() == list(EXPECTED_SOURCE_LINES)
    assert not (tmp_path / "storage").exists()

"""Section 60: scraper.py CLI tests for football ingestion commands."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sports_analytics.data.database import connect_database
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import JobStatus
from sports_analytics.ingestion.types import INGEST_FOOTBALL_DATA_CSV_JOB_TYPE
from tests.helpers import repository_root, scrubbed_subprocess_environ


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


def test_scraper_lists_sources_without_bootstrapping_database(tmp_path: Path) -> None:
    result = _run_scraper(tmp_path, "--list-sources")

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == ["football-data-co-uk"]
    assert not (tmp_path / "storage" / "operational.sqlite3").exists()


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

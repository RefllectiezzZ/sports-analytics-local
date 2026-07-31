from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from sports_analytics.core.runtime import bootstrap_runtime
from sports_analytics.data.codec import dumps_canonical_json, format_utc_timestamp
from sports_analytics.data.database import connect_database
from sports_analytics.models.football_scores import (
    FOOTBALL_SCORE_MODEL_VERSION,
    FootballScoreModel,
    ScoreModelConfiguration,
    ScoreModelDiagnostics,
    write_score_model_artifact,
)
from sports_analytics.mvp.automatic_market_data import (
    AutomaticProviderConfig,
    AutomaticProviderStore,
    run_automatic_acquisition,
)
from sports_analytics.providers.the_odds_api.client import ApiHttpResponse, ProviderSecret
from sports_analytics.services.champion_resolution import write_score_calibration_artifact
from sports_analytics.sports.football.markets import MARKET_KEY_MATCH_RESULT_1X2
from tests.helpers_snapshots import build_verified_participant_registry

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


class FakeOddsTransport:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload, separators=(",", ":")).encode()
        self.calls = 0

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        maximum_bytes: int,
        maximum_redirects: int,
    ) -> ApiHttpResponse:
        del timeout_seconds, headers, maximum_bytes
        assert maximum_redirects == 0
        assert url.startswith("https://api.the-odds-api.com/v4/sports/soccer_epl/odds?")
        self.calls += 1
        return ApiHttpResponse(
            200,
            {
                "x-requests-remaining": "499",
                "x-requests-used": "1",
                "x-requests-last": "1",
            },
            self.payload,
            url,
        )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "settings.toml"
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


def _provider_payload(home: str, away: str) -> list[dict[str, object]]:
    def book(key: str, title: str, prices: tuple[float, float, float]) -> dict[str, object]:
        return {
            "key": key,
            "title": title,
            "last_update": "2026-08-01T12:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-08-01T12:00:00Z",
                    "outcomes": [
                        {"name": home, "price": prices[0]},
                        {"name": "Draw", "price": prices[1]},
                        {"name": away, "price": prices[2]},
                    ],
                }
            ],
        }

    return [
        {
            "id": "epl-event-1",
            "sport_key": "soccer_epl",
            "sport_title": "EPL",
            "commence_time": "2026-08-15T19:00:00Z",
            "home_team": home,
            "away_team": away,
            "bookmakers": [
                book("alpha", "Alpha Book", (2.10, 3.40, 3.60)),
                book("beta", "Beta Book", (2.25, 3.30, 3.55)),
            ],
        }
    ]


def _register_champion(
    *,
    database_path: Path,
    models_root: Path,
    teams: tuple[str, ...],
) -> None:
    model = FootballScoreModel(
        model_family="independent-poisson",
        competition_id="eng-premier-league",
        training_start=date(2024, 1, 1),
        training_end=date(2026, 7, 1),
        teams=tuple(sorted(teams)),
        base_log_rate=0.1,
        home_advantage=0.15,
        attack_strengths=tuple(0.0 for _ in teams),
        defence_strengths=tuple(0.0 for _ in teams),
        rho=0.0,
        configuration=ScoreModelConfiguration(minimum_matches=1),
        diagnostics=ScoreModelDiagnostics(True, 1, 1.0, 0.0, 0),
    )
    artifact = write_score_model_artifact(
        root=models_root,
        relative_directory="automatic-champion",
        model=model,
    )
    calibration = write_score_calibration_artifact(
        root=models_root,
        relative_directory="automatic-champion-calibration",
        model_artifact_id=artifact.artifact_id,
        training_lineage="verified-training-artifact",
        temperature=1.0,
    )
    provenance = {
        "competition_id": "eng-premier-league",
        "model_purpose": "football-fair-odds",
        "probability_generator_scope": "football-score-surface-full-match",
        "evaluation_mode": "prospective-operator",
        "artifact_type": "football-score-model",
        "artifact_schema": "football-score-model-v1",
        "model_family": "independent-poisson",
        "training_lineage": "verified-training-artifact",
        "calibration": {
            "method": "global-temperature",
            "relative_directory": "automatic-champion-calibration",
            "lineage_artifact_id": calibration.artifact_id,
            "lineage_checksum_sha256": calibration.checksum_sha256,
        },
    }
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO model_registry_entries VALUES
            (?, ?, ?, ?, ?, ?, ?, 'champion', 'promoted', ?, 'operator', ?, NULL, 3)
            """,
            (
                artifact.artifact_id,
                artifact.checksum_sha256,
                "automatic-champion",
                FOOTBALL_SCORE_MODEL_VERSION,
                "score-history-v1",
                "football",
                MARKET_KEY_MATCH_RESULT_1X2,
                format_utc_timestamp(NOW),
                dumps_canonical_json(provenance),
            ),
        )


def test_fake_provider_cycle_publishes_and_analyses_idempotently(tmp_path: Path) -> None:
    config_path = _config_file(tmp_path)
    runtime = bootstrap_runtime(
        "automatic_test",
        config_path=config_path,
        base_directory=tmp_path,
    )
    participant_ids = (
        "11111111-1111-5111-8111-111111111111",
        "22222222-2222-5222-8222-222222222222",
    )
    registry_artifact, registry, reference = build_verified_participant_registry(
        tmp_path,
        root=runtime.paths.exports_directory,
        canonical_participant_ids=participant_ids,
        relative_directory="mvp/participant-registries/automatic-test",
        competition_id="eng-premier-league",
        evaluated_at_utc=NOW,
    )
    source = runtime.paths.exports_directory / reference.relative_directory
    destination = runtime.paths.snapshots_directory / reference.relative_directory
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    assert registry_artifact.artifact_id == registry.artifact.artifact_id
    _register_champion(
        database_path=runtime.paths.sqlite_path,
        models_root=runtime.paths.models_directory,
        teams=participant_ids,
    )
    teams = registry.participants_for_competition("eng-premier-league")
    transport = FakeOddsTransport(
        _provider_payload(
            teams[0].canonical_display_name,
            teams[1].canonical_display_name,
        )
    )
    config = AutomaticProviderConfig(
        enabled=True,
        paused=False,
        authentication_blocked=False,
        region="eu",
        competitions=("eng-premier-league",),
        markets=("h2h",),
        refresh_interval_minutes=10,
        quota_reserve=20,
        generation=1,
        updated_at_utc=NOW,
    )
    store = AutomaticProviderStore(runtime.paths.storage_root)
    store.save_config(config)
    store.save_secret(ProviderSecret("fake-integration-key"))

    first = run_automatic_acquisition(
        runtime=runtime,
        config=config,
        secret=ProviderSecret("fake-integration-key"),
        transport=transport,
        clock=lambda: NOW,
    )
    second = run_automatic_acquisition(
        runtime=runtime,
        config=config,
        secret=ProviderSecret("fake-integration-key"),
        transport=transport,
        clock=lambda: NOW,
    )

    assert first["state"] == "succeeded"
    assert first["events_discovered"] == 1
    assert first["events_reconciled"] == 1
    assert first["bookmakers_observed"] == 2
    assert first["valid_quote_count"] == 6
    assert first["product_artifact_ids"]
    assert second["changed"] is False
    state = store.load_state()
    assert state["last_known_good_product_at"] == format_utc_timestamp(NOW)
    ranked = state["ranked_opportunities"]
    assert isinstance(ranked, list)
    assert ranked
    assert all(item["placement_state"] == "manual-only" for item in ranked)
    priced = [item for item in ranked if item["best_offered_price"] is not None]
    assert priced
    assert all(item["bookmaker_coverage_count"] == 2 for item in priced)
    home = next(item for item in priced if item["selection"] == "home")
    assert home["best_bookmaker"] == "Beta Book"
    assert not any("placement" in item for item in state if item.startswith("automatic_"))
    _assert_streamlit_health_and_shutdown(tmp_path=tmp_path, config_path=config_path)


def _assert_streamlit_health_and_shutdown(
    *,
    tmp_path: Path,
    config_path: Path,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    entry = Path("src/sports_analytics/ui/streamlit_entry.py").resolve()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(entry),
            "--server.address=127.0.0.1",
            f"--server.port={port}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
            "--",
            "--config",
            str(config_path),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    healthy = False
    deadline = time.monotonic() + 20.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(f"Streamlit exited before health check: {output[-2000:]}")
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                connection.request("GET", "/_stcore/health")
                response = connection.getresponse()
                body = response.read()
                connection.close()
                if response.status == 200 and body.strip() == b"ok":
                    healthy = True
                    break
            except OSError:
                time.sleep(0.1)
        assert healthy, "Streamlit health endpoint did not become ready"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    assert process.poll() is not None

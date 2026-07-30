from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sports_analytics.artifacts import write_analytical_artifact
from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
    operator_quote_identity_payload,
)
from sports_analytics.core.paths import RuntimePaths, create_runtime_directories
from sports_analytics.mvp.orchestrator import MVPOrchestrator, _analysis_identity
from sports_analytics.services.football_product import (
    FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
    FOOTBALL_PRODUCT_READ_MODEL_TYPE,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _quote(*, line: str = "2.5") -> OperatorQuoteInput:
    return OperatorQuoteInput(
        provider_id="betano-pt",
        provider_display_name="Betano",
        sport_code="football",
        canonical_event_id="event-1",
        market_family="total-goals",
        outcome_key="over",
        line_value=Decimal(line),
        market_period="full-match",
        participant_scope="event",
        canonical_participant_id=None,
        overtime_scope=REGULATION_SCOPE,
        rules_scope=FOOTBALL_RULES_SCOPE,
        offered_decimal_odds=Decimal("1.95"),
        observed_at_utc=NOW,
        valid_until_utc=datetime(2026, 8, 1, 12, 10, tzinfo=UTC),
        source_kind=OperatorQuoteSourceKind.MANUAL,
        operator_note="presentation only",
        import_batch_id="batch-a",
    )


def _identity(quote: OperatorQuoteInput) -> str:
    return _analysis_identity(
        event_artifact_id="events",
        operator_quotes=(quote,),
        champion_artifact_id="champion",
        policy_artifact_id="policy",
    )


def test_line_value_is_material_to_product_run_identity() -> None:
    assert _identity(_quote(line="2.5")) != _identity(_quote(line="3.5"))


def test_rules_and_participant_scope_are_material_to_product_run_identity() -> None:
    quote = _quote()
    assert _identity(quote) != _identity(
        replace(quote, rules_scope="football-alternate-settlement-v1")
    )
    assert _identity(quote) != _identity(
        replace(
            quote,
            participant_scope="home",
            canonical_participant_id="home-team",
        )
    )


def test_identical_semantics_are_idempotent_and_ignore_presentation_text() -> None:
    quote = _quote()
    duplicate = replace(
        quote,
        provider_display_name="BETANO",
        operator_note="second presentation note",
        import_batch_id="batch-b",
    )

    assert operator_quote_identity_payload(quote) == operator_quote_identity_payload(duplicate)
    assert _identity(quote) == _identity(quote) == _identity(duplicate)


def test_repeated_identical_input_reuses_one_product_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    storage = tmp_path / "runtime"
    paths = RuntimePaths(
        base_directory=tmp_path,
        storage_root=storage,
        sqlite_path=storage / "operational.sqlite3",
        raw_directory=storage / "raw",
        snapshots_directory=storage / "snapshots",
        features_directory=storage / "features",
        models_directory=storage / "models",
        exports_directory=storage / "exports",
        logs_directory=storage / "logs",
    )
    create_runtime_directories(paths)
    quote = _quote()
    identity = _analysis_identity(
        event_artifact_id="events",
        operator_quotes=(quote,),
        champion_artifact_id="champion",
        policy_artifact_id="policy",
    )
    relative = f"mvp/product-runs/eng-premier-league/{identity}/read-model"
    existing = write_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
        schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
        payload={"product_state": {"operational_state": "production-eligible"}},
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "_champion_for",
        lambda _self, _paths, _competition: (
            "eng-premier-league",
            "football.match-result.1x2.full-match",
            "champion",
        ),
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "_ensure_default_policy",
        lambda _self, _paths: (SimpleNamespace(artifact_id="policy"), None),
    )
    monkeypatch.setattr(
        "sports_analytics.mvp.orchestrator.run_and_publish_production_football_product",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("an identical persisted run must be reused")
        ),
    )
    orchestrator = MVPOrchestrator(base_directory=tmp_path)
    arguments = {
        "paths": paths,
        "registry": SimpleNamespace(),
        "event_artifact": SimpleNamespace(artifact_id="events"),
        "events": (SimpleNamespace(competition_id="eng-premier-league"),),
        "operator_quotes": (quote,),
        "evaluated_at_utc": NOW,
    }

    first = orchestrator._run_analysis(**arguments)
    second = orchestrator._run_analysis(**arguments)

    assert first.read_model_artifact.artifact_id == existing.artifact_id
    assert second.read_model_artifact.artifact_id == existing.artifact_id
    run_root = paths.exports_directory / Path(relative).parent
    assert len(tuple(run_root.rglob("manifest.json"))) == 1

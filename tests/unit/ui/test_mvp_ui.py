from __future__ import annotations

import inspect
from pathlib import Path

from streamlit.testing.v1 import AppTest

from sports_analytics.mvp.orchestrator import MVPOrchestrator
from sports_analytics.ui import mvp_pages
from sports_analytics.ui.mvp_pages import MVP_PAGES


def test_primary_navigation_and_safety_language() -> None:
    assert MVP_PAGES == ("Dashboard", "Bets", "Matches", "Odds", "History", "System")
    source = inspect.getsource(mvp_pages)
    for required in (
        "Matches analysed",
        "Placeable manual proposals",
        "Ready for manual placement",
        "Analytical candidates",
        "Accumulators",
        "hold reason",
    ):
        assert required in source
    for prohibited in (
        "automatic placement",
        "form_submit_button",
        "subprocess",
        "os.system",
    ):
        assert prohibited not in source


def test_main_dashboard_has_no_raw_json_renderer() -> None:
    source = inspect.getsource(mvp_pages.render_dashboard)
    assert "st.json" not in source
    assert "run_every" in source
    assert "while True" not in source
    assert source.index('if st.button(\n            "Prepare system"') < source.index(
        "orchestrator.prepare_system()"
    )


def test_ui_has_no_forced_model_or_promotion_override() -> None:
    source = inspect.getsource(mvp_pages)
    for prohibited in (
        "model_artifact_id",
        "champion selector",
        "force promotion",
        "apply_promotion",
    ):
        assert prohibited not in source


def test_empty_runtime_renders_guided_dashboard(tmp_path: Path, monkeypatch) -> None:
    from tests.helpers import repository_root

    preparation_calls = 0

    def unexpected_preparation(_self) -> None:
        nonlocal preparation_calls
        preparation_calls += 1
        raise AssertionError("Streamlit refresh must not prepare or promote")

    monkeypatch.setattr(MVPOrchestrator, "prepare_system", unexpected_preparation)
    monkeypatch.chdir(tmp_path)
    app = AppTest.from_file(str(repository_root() / "app.py"))
    app.run(timeout=15)

    assert not app.exception
    assert preparation_calls == 0
    assert any("Sports Analytics Local" in item.value for item in app.title)
    assert any("Runtime initialized" in item.value for item in app.markdown)

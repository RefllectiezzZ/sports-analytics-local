"""Streamlit application entry point with shared CLI compatibility."""

from collections.abc import Sequence

from sports_analytics.core.cli import run_component
from sports_analytics.ui.application import run_streamlit_app, running_in_streamlit


def main(argv: Sequence[str] | None = None) -> int:
    """Run shared configuration/database CLI modes outside Streamlit."""
    return run_component(
        "app",
        "Local Streamlit application entry point. Launch with `streamlit run app.py`.",
        argv=argv,
    )


if __name__ == "__main__":
    if running_in_streamlit():
        run_streamlit_app()
    else:
        raise SystemExit(main())

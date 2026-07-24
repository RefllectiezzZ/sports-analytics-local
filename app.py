"""Streamlit application entry point (placeholder)."""

from collections.abc import Sequence

from sports_analytics.core.cli import run_component


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap the app component and report that Streamlit is not implemented."""
    return run_component(
        "app",
        "Local Streamlit application entry point (placeholder).",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())

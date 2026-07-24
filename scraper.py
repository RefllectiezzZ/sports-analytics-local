"""Data ingestion coordinator entry point (placeholder)."""

from collections.abc import Sequence

from sports_analytics.core.cli import run_component


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap the scraper component and report that ingestion is not implemented."""
    return run_component(
        "scraper",
        "Data ingestion coordinator entry point (placeholder).",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())

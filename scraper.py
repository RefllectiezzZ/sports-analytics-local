"""Data ingestion coordinator entry point."""

from collections.abc import Sequence

from sports_analytics.ingestion.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scraper CLI."""
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

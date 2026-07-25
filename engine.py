"""Analytics engine entry point."""

from collections.abc import Sequence

from sports_analytics.services.engine_cli import main as engine_main


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap the analytics engine CLI."""
    return engine_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

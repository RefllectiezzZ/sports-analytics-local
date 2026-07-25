"""Local startup coordinator entry point."""

from collections.abc import Sequence

from sports_analytics.local.supervisor import main as supervisor_main


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local worker process supervisor CLI."""
    return supervisor_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

"""Background worker entry point."""

from collections.abc import Sequence

from sports_analytics.jobs.cli import main as worker_main


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local durable-job worker CLI."""
    return worker_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

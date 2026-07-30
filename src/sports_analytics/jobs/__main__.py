"""Package-native durable worker entry point."""

from sports_analytics.jobs.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

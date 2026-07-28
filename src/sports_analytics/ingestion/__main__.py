"""Module entry point for ``python -m sports_analytics.ingestion``.

Prefer the documented root script:

    .venv\\Scripts\\python.exe scraper.py ...
"""

from __future__ import annotations

from sports_analytics.ingestion.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

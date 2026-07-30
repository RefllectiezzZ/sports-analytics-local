"""Package-native Streamlit script for the installed local v1 application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sports_analytics.ui.application import run_streamlit_app


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--env-file")
    return parser.parse_args(sys.argv[1:])


arguments = _arguments()
run_streamlit_app(
    base_directory=Path.cwd(),
    config_path=arguments.config,
    env_file=arguments.env_file,
)

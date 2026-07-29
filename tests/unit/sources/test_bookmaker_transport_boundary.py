"""Static production boundary against independent bookmaker endpoint replay."""

from __future__ import annotations

import ast
from pathlib import Path


def test_bookmaker_production_modules_have_no_independent_http_client() -> None:
    source_root = Path(__file__).parents[3] / "src" / "sports_analytics"
    roots = (
        source_root / "bookmakers",
        source_root / "sources" / "betano",
        source_root / "sources" / "betclic",
        source_root / "sources" / "bookmaker_extraction",
        source_root / "sources" / "browser",
    )
    forbidden_import_roots = {
        "aiohttp",
        "httpx",
        "requests",
        "urllib.request",
    }
    forbidden_attribute_names = {
        "extra_http_headers",
        "launch_persistent_context",
        "new_context.request",
        "set_extra_http_headers",
        "storage_state",
    }

    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            imports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
            assert not any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for imported in imports
                for forbidden in forbidden_import_roots
            ), path
            assert not any(marker in source for marker in forbidden_attribute_names), path

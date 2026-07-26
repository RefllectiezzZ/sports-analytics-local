"""Streamlit application shell and read-only dependency wiring."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from sports_analytics.core.exceptions import ArtifactError, ConfigurationError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.ui.catalogue import (
    ArtifactCatalogueEntry,
    discover_typed_artifacts,
    load_catalogue_artifact,
)
from sports_analytics.ui.pages import PAGES, render_page
from sports_analytics.ui.theme import apply_theme
from sports_analytics.ui.view_models import build_artifact_summary, short_identifier


def running_in_streamlit() -> bool:
    """Return whether the module is executing inside a Streamlit script context."""
    from streamlit.runtime.scriptrunner import get_script_run_ctx

    return get_script_run_ctx(suppress_warning=True) is not None


def run_streamlit_app(*, base_directory: Path | None = None) -> None:
    """Render the local read-only artifact browser."""
    st.set_page_config(
        page_title="Sports analytics workspace",
        page_icon="◫",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_theme()
    repository_base = (base_directory or Path.cwd()).resolve()
    try:
        settings = load_settings(base_directory=repository_base)
        paths = resolve_paths(settings, repository_base)
    except ConfigurationError as exc:
        st.title("Sports analytics workspace")
        st.error(f"Configuration could not be loaded: {exc}")
        st.info(
            "Correct the local configuration and rerun the application. No artifact "
            "or database was modified."
        )
        return

    catalogue = discover_typed_artifacts(paths.exports_directory)
    valid = tuple(entry for entry in catalogue if entry.is_valid)
    invalid = tuple(entry for entry in catalogue if not entry.is_valid)

    st.sidebar.title("Sports analytics")
    st.sidebar.caption("Verified persisted artifacts · read-only")
    page = st.sidebar.radio("Navigation", PAGES, key="sal_page")
    _render_invalid_entries(invalid)
    if not valid:
        st.title("Sports analytics workspace")
        st.info(
            "No valid typed analysis or backtest artifacts were found under the "
            "configured exports directory."
        )
        if invalid:
            st.warning(
                "One or more artifact candidates failed validation. Review the "
                "validation details in the sidebar; none were trusted."
            )
        st.markdown(
            """
This application never generates fallback data. Publish a typed analytical artifact
through the existing engine workflow, then rerun or refresh this page.
"""
        )
        return

    labels = {_artifact_label(entry): entry for entry in valid}
    selected_label = st.sidebar.selectbox(
        "Selected artifact",
        tuple(labels),
        key="sal_artifact",
    )
    if selected_label is None:
        st.info("Choose a verified artifact from the sidebar.")
        return
    selected = labels[selected_label]
    try:
        artifact = load_catalogue_artifact(
            root=paths.exports_directory,
            entry=selected,
        )
    except ArtifactError as exc:
        st.title("Artifact validation error")
        st.error(f"`{selected.relative_directory}` could not be revalidated: {exc}")
        st.info("Select another artifact or repair the persisted artifact outside the UI.")
        return

    summary = build_artifact_summary(artifact)
    with st.sidebar.container(border=True):
        st.markdown(f"**{artifact.artifact_kind.title()} artifact**")
        st.caption(artifact.schema_version)
        st.code(short_identifier(artifact.artifact_id), language=None)
        st.caption(summary.warning.title)
        st.caption(artifact.relative_directory)
    st.sidebar.info(
        "Historical and synthetic rows are analytical records, not current executable offers."
    )
    render_page(page, artifact)


def _render_invalid_entries(entries: tuple[ArtifactCatalogueEntry, ...]) -> None:
    if not entries:
        return
    with st.sidebar.expander(f"Validation issues · {len(entries)}"):
        for entry in entries:
            st.error(entry.relative_directory)
            st.caption(entry.validation_error or "Unknown validation failure")


def _artifact_label(entry: ArtifactCatalogueEntry) -> str:
    return (
        f"{entry.artifact_kind} · {entry.schema_version} · "
        f"{entry.relative_directory} · {short_identifier(entry.artifact_id or '')}"
    )

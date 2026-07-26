"""Small deterministic visual theme for the Streamlit interface."""

from __future__ import annotations

from typing import Final

_ORB_LAYER: Final[str] = """
<div class="sal-ambient" aria-hidden="true">
  <span class="sal-orb sal-orb-one"></span>
  <span class="sal-orb sal-orb-two"></span>
  <span class="sal-orb sal-orb-three"></span>
</div>
""".strip()


def theme_css() -> str:
    """Return deterministic, scoped CSS with accessible reduced-motion handling."""
    return """
<style>
:root {
  --sal-accent: #4f6f8f;
  --sal-accent-soft: rgba(79, 111, 143, 0.14);
  --sal-border: color-mix(in srgb, currentColor 16%, transparent);
  --sal-surface: color-mix(in srgb, var(--background-color, #f5f6f8) 91%, transparent);
  --sal-shadow: 0 12px 30px rgba(25, 35, 50, 0.08);
}

[data-testid="stAppViewContainer"] {
  overflow-x: hidden;
  isolation: isolate;
}

[data-testid="stAppViewContainer"] > .main,
[data-testid="stSidebar"] {
  position: relative;
  z-index: 2;
}

[data-testid="stSidebar"] {
  border-right: 1px solid var(--sal-border);
  backdrop-filter: blur(18px);
}

.sal-ambient {
  position: fixed;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  contain: strict;
}

.sal-orb {
  position: absolute;
  display: block;
  width: min(34rem, 55vw);
  aspect-ratio: 1;
  border-radius: 50%;
  opacity: 0.13;
  filter: blur(72px);
  will-change: transform, opacity;
}

.sal-orb-one {
  top: -13rem;
  right: -8rem;
  background: radial-gradient(circle, #718da8 0%, rgba(113, 141, 168, 0) 70%);
  animation: sal-drift-one 29s ease-in-out infinite alternate;
}

.sal-orb-two {
  bottom: -17rem;
  left: -9rem;
  background: radial-gradient(circle, #82968e 0%, rgba(130, 150, 142, 0) 72%);
  animation: sal-drift-two 35s ease-in-out infinite alternate;
}

.sal-orb-three {
  top: 38%;
  left: 48%;
  width: min(24rem, 38vw);
  opacity: 0.08;
  background: radial-gradient(circle, #938ba2 0%, rgba(147, 139, 162, 0) 72%);
  animation: sal-drift-three 24s ease-in-out infinite alternate;
}

@keyframes sal-drift-one {
  from { transform: translate3d(0, 0, 0) scale(0.96); }
  to { transform: translate3d(-2.5rem, 2rem, 0) scale(1.04); }
}

@keyframes sal-drift-two {
  from { transform: translate3d(0, 0, 0) scale(1); }
  to { transform: translate3d(2.25rem, -1.75rem, 0) scale(1.05); }
}

@keyframes sal-drift-three {
  from { transform: translate3d(-50%, -1rem, 0) scale(0.98); }
  to { transform: translate3d(calc(-50% + 1.5rem), 1rem, 0) scale(1.03); }
}

[data-testid="stMetric"],
[data-testid="stVerticalBlockBorderWrapper"] {
  border-color: var(--sal-border);
  border-radius: 0.9rem;
  background: linear-gradient(145deg, var(--sal-accent-soft), transparent 58%);
  transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
}

[data-testid="stMetric"] {
  padding: 0.85rem 1rem;
  animation: sal-card-enter 260ms ease-out both;
}

[data-testid="stMetric"]:hover,
[data-testid="stVerticalBlockBorderWrapper"]:hover {
  transform: translateY(-1px);
  border-color: color-mix(in srgb, var(--sal-accent) 48%, transparent);
  box-shadow: var(--sal-shadow);
}

@keyframes sal-card-enter {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

:where(button, input, select, textarea, [tabindex]):focus-visible {
  outline: 3px solid color-mix(in srgb, var(--sal-accent) 72%, transparent) !important;
  outline-offset: 2px;
}

[data-testid="stDataFrame"] {
  border: 1px solid var(--sal-border);
  border-radius: 0.75rem;
  overflow: hidden;
}

@media (prefers-reduced-motion: reduce) {
  .sal-orb,
  [data-testid="stMetric"],
  [data-testid="stVerticalBlockBorderWrapper"] {
    animation: none !important;
    transition: none !important;
    transform: none !important;
  }
}
</style>
""".strip()


def apply_theme() -> None:
    """Apply theme and decorative layer idempotently on each Streamlit rerun."""
    import streamlit as st

    st.markdown(theme_css(), unsafe_allow_html=True)
    st.markdown(_ORB_LAYER, unsafe_allow_html=True)

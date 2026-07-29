"""Disabled-by-default production hook for provider-owned Stage-B navigation."""

from __future__ import annotations

from dataclasses import replace

from sports_analytics.bookmakers.navigation import (
    NavigationPlanExecutor,
    StageBNavigationCapability,
)
from sports_analytics.bookmakers.window import AcquisitionWindow
from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult


def execute_optional_stage_b(
    *,
    capability: StageBNavigationCapability,
    stage_a_result: BrowserAcquisitionResult,
    stage_a_bundle: ProviderAcquisitionBundle,
    acquisition_window: AcquisitionWindow | None,
    executor: NavigationPlanExecutor | None,
) -> BrowserAcquisitionResult | None:
    """Execute reviewed Stage-B navigation only when the exact capability is enabled."""
    identity = (stage_a_result.provider_id, stage_a_result.sport)
    if (stage_a_bundle.provider_id, stage_a_bundle.sport) != identity:
        msg = "Stage-A bundle provider/sport does not match the browser result"
        raise PermanentSourceError(msg)
    if (capability.provider_id, capability.sport) != identity:
        msg = "Stage-B capability provider/sport mismatch"
        raise PermanentSourceError(msg)
    if not capability.enabled:
        return None
    if acquisition_window is None or executor is None:
        msg = "enabled Stage-B capability requires a window and executor"
        raise PermanentSourceError(msg)
    candidates = capability.candidates(
        stage_a_result=stage_a_result,
        stage_a_bundle=stage_a_bundle,
    )
    plan = capability.build_plan(
        candidates=candidates,
        acquisition_window=acquisition_window,
    )
    if (
        plan.provider_id != stage_a_result.provider_id
        or plan.sport != stage_a_result.sport
        or plan.acquisition_window != acquisition_window
    ):
        msg = "Stage-B plan identity does not match the active acquisition"
        raise PermanentSourceError(msg)
    if not plan.targets:
        return None
    for target in plan.targets:
        capability.validate_target(target)
    result = executor.execute(plan)
    if not isinstance(result, BrowserAcquisitionResult):
        msg = "Stage-B executor must return BrowserAcquisitionResult"
        raise PermanentSourceError(msg)
    if (
        result.provider_id != stage_a_result.provider_id
        or result.sport != stage_a_result.sport
        or result.acquisition_cycle_id != stage_a_result.acquisition_cycle_id
    ):
        msg = "Stage-B browser evidence does not match the active acquisition"
        raise PermanentSourceError(msg)
    return result


def merge_stage_a_and_b_results(
    stage_a: BrowserAcquisitionResult,
    stage_b: BrowserAcquisitionResult,
) -> BrowserAcquisitionResult:
    """Merge linked browser evidence without persisting ephemeral navigation URLs."""
    if (
        stage_a.provider_id,
        stage_a.sport,
        stage_a.acquisition_cycle_id,
    ) != (
        stage_b.provider_id,
        stage_b.sport,
        stage_b.acquisition_cycle_id,
    ):
        msg = "browser acquisition stages have mismatched identities"
        raise PermanentSourceError(msg)
    return replace(
        stage_a,
        observed_at_utc=max(stage_a.observed_at_utc, stage_b.observed_at_utc),
        pages=stage_a.pages + stage_b.pages,
        responses=stage_a.responses + stage_b.responses,
        diagnostics=stage_a.diagnostics + stage_b.diagnostics,
        block_reason=stage_b.block_reason or stage_a.block_reason,
        warnings=tuple(sorted(set(stage_a.warnings) | set(stage_b.warnings))),
        cookie_banner_dismissed=(
            stage_a.cookie_banner_dismissed or stage_b.cookie_banner_dismissed
        ),
        network_metadata=stage_a.network_metadata + stage_b.network_metadata,
        grpc_web_diagnostics=(stage_a.grpc_web_diagnostics + stage_b.grpc_web_diagnostics),
    )

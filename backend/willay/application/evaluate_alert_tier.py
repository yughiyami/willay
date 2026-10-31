"""Use case: evaluate the alert tier. Purely deterministic; no LLM involvement."""

from __future__ import annotations

from willay.domain.entities import HazardContext
from willay.domain.tiers import TierConfig, TierEvaluation, evaluate_tier


def evaluate_alert_tier(
    context: HazardContext, site_static_risk: float, config: TierConfig | None = None
) -> TierEvaluation:
    return evaluate_tier(context, site_static_risk, config)

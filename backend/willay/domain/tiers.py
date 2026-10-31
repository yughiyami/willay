"""Deterministic alert tier rules.

Alert levels are decided here and only here. LLMs never decide tiers.
"""

from __future__ import annotations

from dataclasses import dataclass

from willay.domain.entities import (
    AlertTier,
    EnfenMagnitude,
    HazardContext,
    RainfallAnomaly,
    SenamhiLevel,
)


@dataclass(frozen=True)
class TierConfig:
    """Configurable thresholds for the three alert horizons."""

    min_enfen_magnitude: EnfenMagnitude = EnfenMagnitude.STRONG
    preparation_site_risk_threshold: float = 0.5
    attention_min_senamhi_level: SenamhiLevel = SenamhiLevel.ORANGE
    attention_rain_72h_mm: float = 60.0
    evacuation_rain_24h_mm: float = 20.0
    evacuation_water_rise_cm: float = 30.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.preparation_site_risk_threshold <= 1.0:
            raise ValueError("preparation_site_risk_threshold must be within [0, 1]")
        for name in ("attention_rain_72h_mm", "evacuation_rain_24h_mm", "evacuation_water_rise_cm"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True)
class TierEvaluation:
    """Result of tier evaluation.

    `tier` is the highest-priority outcome. `triggered` lists every horizon whose
    conditions hold (PENDING_CONFIRMATION is never "triggered" as EVACUATION).
    """

    tier: AlertTier
    triggered: tuple[AlertTier, ...]
    reasons: tuple[str, ...]


def _preparation(ctx: HazardContext, site_static_risk: float, cfg: TierConfig) -> list[str] | None:
    if (
        ctx.enfen_magnitude >= cfg.min_enfen_magnitude
        and ctx.seasonal_rainfall_anomaly is RainfallAnomaly.ABOVE_NORMAL
        and site_static_risk >= cfg.preparation_site_risk_threshold
    ):
        return [
            f"ENFEN magnitude {ctx.enfen_magnitude.value} >= {cfg.min_enfen_magnitude.value}",
            "seasonal rainfall anomaly forecast is above_normal",
            f"site static risk {site_static_risk:.2f} >= {cfg.preparation_site_risk_threshold:.2f}",
        ]
    return None


def _attention(ctx: HazardContext, cfg: TierConfig) -> list[str] | None:
    reasons: list[str] = []
    if ctx.senamhi_level >= cfg.attention_min_senamhi_level:
        reasons.append(f"SENAMHI warning level is {ctx.senamhi_level.value}")
    if ctx.forecast_rain_72h_mm >= cfg.attention_rain_72h_mm:
        reasons.append(
            f"forecast rain next 72h {ctx.forecast_rain_72h_mm:.1f} mm >= {cfg.attention_rain_72h_mm:.1f} mm"
        )
    return reasons or None


def _evacuation_conditions(ctx: HazardContext, cfg: TierConfig) -> list[str] | None:
    if ctx.observed_rain_24h_mm < cfg.evacuation_rain_24h_mm:
        return None
    reasons = [f"observed rain 24h {ctx.observed_rain_24h_mm:.1f} mm >= {cfg.evacuation_rain_24h_mm:.1f} mm"]
    water_rise_ok = ctx.water_rise_cm is not None and ctx.water_rise_cm >= cfg.evacuation_water_rise_cm
    if water_rise_ok:
        reasons.append(f"measured water rise {ctx.water_rise_cm:.0f} cm >= {cfg.evacuation_water_rise_cm:.0f} cm")
    if ctx.watcher_report:
        reasons.append("community watcher report received")
    if not (water_rise_ok or ctx.watcher_report):
        return None
    return reasons


def evaluate_tier(
    context: HazardContext, site_static_risk: float, config: TierConfig | None = None
) -> TierEvaluation:
    """Evaluate the alert tier for one site.

    EVACUATION requires explicit operator confirmation (human in the loop).
    Without it the result is PENDING_CONFIRMATION, never EVACUATION.
    """
    cfg = config or TierConfig()
    triggered: list[AlertTier] = []
    reasons: list[str] = []

    prep = _preparation(context, site_static_risk, cfg)
    if prep:
        triggered.append(AlertTier.PREPARATION)
        reasons += [f"[PREPARATION] {r}" for r in prep]

    attention = _attention(context, cfg)
    if attention:
        triggered.append(AlertTier.ATTENTION)
        reasons += [f"[ATTENTION] {r}" for r in attention]

    evac = _evacuation_conditions(context, cfg)
    if evac:
        if context.operator_confirmed:
            triggered.append(AlertTier.EVACUATION)
            reasons += [f"[EVACUATION] {r}" for r in evac]
            reasons.append("[EVACUATION] operator confirmed")
            tier = AlertTier.EVACUATION
        else:
            reasons += [f"[PENDING_CONFIRMATION] {r}" for r in evac]
            reasons.append("[PENDING_CONFIRMATION] awaiting explicit operator confirmation")
            tier = AlertTier.PENDING_CONFIRMATION
    elif AlertTier.ATTENTION in triggered:
        tier = AlertTier.ATTENTION
    elif AlertTier.PREPARATION in triggered:
        tier = AlertTier.PREPARATION
    else:
        tier = AlertTier.NONE

    return TierEvaluation(tier=tier, triggered=tuple(triggered), reasons=tuple(reasons))

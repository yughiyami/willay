"""Risk score: Trigger(t) x Susceptibility x TailingsVulnerability x DownstreamExposure.

Every factor is normalized to [0, 1]. A small configurable epsilon floor keeps one
missing or zero factor from silently zeroing the whole score; both the product and the
per-factor breakdown are exposed so explanations can cite them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from willay.domain.entities import (
    EnfenMagnitude,
    HazardContext,
    RainfallAnomaly,
    RiskLevel,
    SenamhiLevel,
    SiteStatus,
    TailingsSite,
)

FACTOR_NAMES = ("trigger", "susceptibility", "tailings_vulnerability", "downstream_exposure")


@dataclass(frozen=True)
class RiskConfig:
    epsilon: float = 0.05
    # tailings volume normalization (log10 scale)
    volume_min_m3: float = 1e3
    volume_max_m3: float = 1e8
    # distance-to-river decay scale
    river_distance_scale_m: float = 500.0
    # tailings vulnerability sub-weights (normalized internally)
    weight_volume: float = 0.35
    weight_status: float = 0.20
    weight_declared_risk: float = 0.25
    weight_river_distance: float = 0.20
    # trigger references for short-term rainfall saturation
    reference_rain_24h_mm: float = 50.0
    reference_rain_72h_mm: float = 150.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.epsilon < 1.0:
            raise ValueError("epsilon must be within [0, 1)")
        if self.volume_min_m3 <= 0 or self.volume_max_m3 <= self.volume_min_m3:
            raise ValueError("volume range must satisfy 0 < min < max")
        if self.river_distance_scale_m <= 0:
            raise ValueError("river_distance_scale_m must be > 0")
        if self.reference_rain_24h_mm <= 0 or self.reference_rain_72h_mm <= 0:
            raise ValueError("reference rain values must be > 0")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# --- Tailings vulnerability sub-factors -------------------------------------


def normalize_log_volume(volume_m3: float, config: RiskConfig) -> float:
    if volume_m3 <= 0:
        return 0.0
    lo, hi = math.log10(config.volume_min_m3), math.log10(config.volume_max_m3)
    return _clamp01((math.log10(volume_m3) - lo) / (hi - lo))


_STATUS_FACTOR = {
    SiteStatus.ACTIVE: 0.4,
    SiteStatus.INACTIVE: 0.7,
    SiteStatus.ABANDONED: 1.0,
}


def status_factor(status: SiteStatus) -> float:
    """Abandoned sites are the worst: no operator, no monitoring, no maintenance."""
    return _STATUS_FACTOR[status]


_DECLARED_RISK_FACTOR = {
    RiskLevel.LOW: 0.25,
    RiskLevel.MEDIUM: 0.5,
    RiskLevel.HIGH: 0.75,
    RiskLevel.VERY_HIGH: 1.0,
}


def declared_risk_factor(level: RiskLevel) -> float:
    return _DECLARED_RISK_FACTOR[level]


def distance_to_river_factor(distance_m: float, config: RiskConfig) -> float:
    """Closer to the river is worse: exp(-d / scale)."""
    return math.exp(-max(0.0, distance_m) / config.river_distance_scale_m)


def tailings_vulnerability(site: TailingsSite, config: RiskConfig) -> float:
    weights = (
        config.weight_volume,
        config.weight_status,
        config.weight_declared_risk,
        config.weight_river_distance,
    )
    parts = (
        normalize_log_volume(site.volume_m3, config),
        status_factor(site.status),
        declared_risk_factor(site.declared_risk),
        distance_to_river_factor(site.distance_to_river_m, config),
    )
    total = sum(weights)
    return _clamp01(sum(w * p for w, p in zip(weights, parts)) / total)


# --- Trigger ---------------------------------------------------------------

_ANOMALY_FACTOR = {
    RainfallAnomaly.BELOW_NORMAL: 0.0,
    RainfallAnomaly.NORMAL: 0.25,
    RainfallAnomaly.ABOVE_NORMAL: 1.0,
}

_SENAMHI_FACTOR = {
    SenamhiLevel.GREEN: 0.0,
    SenamhiLevel.YELLOW: 0.33,
    SenamhiLevel.ORANGE: 0.66,
    SenamhiLevel.RED: 1.0,
}


def trigger_factor(context: HazardContext, config: RiskConfig) -> float:
    """Trigger(t) in [0, 1]: the larger of the seasonal and the short-term signal.

    seasonal   = mean(ENFEN magnitude index, seasonal rainfall anomaly category)
    short-term = max(SENAMHI level, observed rain 24h / ref, forecast rain 72h / ref)
    """
    enfen_norm = list(EnfenMagnitude).index(context.enfen_magnitude) / (len(EnfenMagnitude) - 1)
    seasonal = (enfen_norm + _ANOMALY_FACTOR[context.seasonal_rainfall_anomaly]) / 2.0
    short_term = max(
        _SENAMHI_FACTOR[context.senamhi_level],
        _clamp01(context.observed_rain_24h_mm / config.reference_rain_24h_mm),
        _clamp01(context.forecast_rain_72h_mm / config.reference_rain_72h_mm),
    )
    return _clamp01(max(seasonal, short_term))


# --- Score -----------------------------------------------------------------


@dataclass(frozen=True)
class FactorBreakdown:
    raw: float | None
    value: float
    floored: bool
    missing: bool

    def to_dict(self) -> dict:
        return {"raw": self.raw, "value": self.value, "floored": self.floored, "missing": self.missing}


@dataclass(frozen=True)
class RiskScore:
    value: float
    factors: dict[str, FactorBreakdown] = field(default_factory=dict)

    @property
    def missing_factors(self) -> tuple[str, ...]:
        return tuple(name for name, f in self.factors.items() if f.missing)

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "factors": {name: f.to_dict() for name, f in self.factors.items()},
            "missing_factors": list(self.missing_factors),
        }


def _breakdown(raw: float | None, epsilon: float) -> FactorBreakdown:
    if raw is None:
        return FactorBreakdown(raw=None, value=epsilon, floored=False, missing=True)
    clamped = _clamp01(raw)
    floored = clamped < epsilon
    return FactorBreakdown(raw=raw, value=max(clamped, epsilon), floored=floored, missing=False)


def compute_risk_score(
    *,
    trigger: float | None,
    susceptibility: float | None,
    vulnerability: float | None,
    exposure: float | None,
    config: RiskConfig | None = None,
) -> RiskScore:
    cfg = config or RiskConfig()
    factors = {
        "trigger": _breakdown(trigger, cfg.epsilon),
        "susceptibility": _breakdown(susceptibility, cfg.epsilon),
        "tailings_vulnerability": _breakdown(vulnerability, cfg.epsilon),
        "downstream_exposure": _breakdown(exposure, cfg.epsilon),
    }
    product = 1.0
    for factor in factors.values():
        product *= factor.value
    return RiskScore(value=product, factors=factors)


def compute_static_risk(
    *,
    susceptibility: float | None,
    vulnerability: float | None,
    exposure: float | None,
    config: RiskConfig | None = None,
) -> float:
    """Site static risk: the score without the time-varying Trigger factor."""
    cfg = config or RiskConfig()
    product = 1.0
    for raw in (susceptibility, vulnerability, exposure):
        product *= _breakdown(raw, cfg.epsilon).value
    return product

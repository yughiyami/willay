"""Downstream exposure along a flow path (pure functions, no GIS libraries)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from willay.domain.entities import FlowPath, Settlement
from willay.domain.geo import haversine_m, project_point_on_segment


@dataclass(frozen=True)
class ExposureConfig:
    snap_tolerance_m: float = 500.0
    decay_length_m: float = 10_000.0
    cutoff_m: float = 50_000.0
    reference_population: float = 10_000.0
    water_intake_bonus: float = 0.3
    household_size: float = 4.0

    def __post_init__(self) -> None:
        if self.snap_tolerance_m < 0:
            raise ValueError("snap_tolerance_m must be >= 0")
        if self.decay_length_m <= 0:
            raise ValueError("decay_length_m must be > 0")
        if self.cutoff_m <= 0:
            raise ValueError("cutoff_m must be > 0")
        if self.reference_population <= 0:
            raise ValueError("reference_population must be > 0")


@dataclass(frozen=True)
class SettlementExposure:
    settlement_id: str
    settlement_name: str
    along_path_m: float
    snap_distance_m: float
    weight: float
    impact: float
    population: int
    households: int
    has_water_intake: bool

    def to_dict(self) -> dict:
        return {
            "settlement_id": self.settlement_id,
            "settlement_name": self.settlement_name,
            "along_path_km": self.along_path_m / 1000.0,
            "snap_distance_m": self.snap_distance_m,
            "weight": self.weight,
            "impact": self.impact,
            "population": self.population,
            "households": self.households,
            "has_water_intake": self.has_water_intake,
        }


@dataclass(frozen=True)
class ExposureResult:
    value: float
    contributions: tuple[SettlementExposure, ...]

    @property
    def nearest(self) -> SettlementExposure | None:
        return self.contributions[0] if self.contributions else None


def _snap_to_path(path: FlowPath, settlement: Settlement) -> tuple[float, float]:
    """Return (along_path_m, snap_distance_m) of the closest point on the path."""
    best_snap = math.inf
    best_along = 0.0
    origin = path.points[0].cumulative_distance_m
    pts = path.points
    for a, b in zip(pts, pts[1:]):
        (qx, qy), t = project_point_on_segment(settlement.lon, settlement.lat, a.lon, a.lat, b.lon, b.lat)
        snap = haversine_m(settlement.lon, settlement.lat, qx, qy)
        if snap < best_snap:
            best_snap = snap
            best_along = a.cumulative_distance_m + t * (b.cumulative_distance_m - a.cumulative_distance_m) - origin
    return best_along, best_snap


def _magnitude(settlement: Settlement, cfg: ExposureConfig) -> float:
    people = max(settlement.population, settlement.households * cfg.household_size)
    base = min(1.0, math.log1p(people) / math.log1p(cfg.reference_population))
    if settlement.has_water_intake:
        base += cfg.water_intake_bonus
    return min(1.0, base)


def downstream_exposure(
    path: FlowPath, settlements: Iterable[Settlement] | Sequence[Settlement], config: ExposureConfig | None = None
) -> ExposureResult:
    """Exposure in [0, 1] of settlements along the downstream flow path.

    Each settlement within `snap_tolerance_m` of the path and within `cutoff_m` along it
    contributes impact = exp(-d / L) * magnitude(population, households, water intake).
    Impacts combine as a noisy-OR so the aggregate saturates at 1.
    """
    cfg = config or ExposureConfig()
    contributions: list[SettlementExposure] = []
    for s in settlements:
        along, snap = _snap_to_path(path, s)
        if snap > cfg.snap_tolerance_m or along > cfg.cutoff_m:
            continue
        weight = math.exp(-along / cfg.decay_length_m)
        contributions.append(
            SettlementExposure(
                settlement_id=s.id,
                settlement_name=s.name,
                along_path_m=along,
                snap_distance_m=snap,
                weight=weight,
                impact=weight * _magnitude(s, cfg),
                population=s.population,
                households=s.households,
                has_water_intake=s.has_water_intake,
            )
        )
    contributions.sort(key=lambda c: c.along_path_m)
    survival = 1.0
    for c in contributions:
        survival *= 1.0 - min(1.0, c.impact)
    return ExposureResult(value=1.0 - survival, contributions=tuple(contributions))

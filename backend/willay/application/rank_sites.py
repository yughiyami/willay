"""Use case: rank tailings sites by risk score, with full factor breakdown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from willay.domain.entities import HazardContext, Settlement, TailingsSite, WaterIntake
from willay.domain.exposure import ExposureConfig, ExposureResult, downstream_exposure
from willay.domain.risk import (
    RiskConfig,
    RiskScore,
    compute_risk_score,
    compute_static_risk,
    tailings_vulnerability,
    trigger_factor,
)
from willay.ports.site_repository import SiteRepositoryPort


@dataclass(frozen=True)
class RankedSite:
    site: TailingsSite
    rank: int
    score: RiskScore
    static_risk: float
    vulnerability: float
    exposure: ExposureResult | None

    def to_dict(self, include_exposure_detail: bool = True) -> dict:
        data = {
            "id": self.site.id,
            "name": self.site.name,
            "lon": self.site.lon,
            "lat": self.site.lat,
            "district": self.site.district,
            "province": self.site.province,
            "status": self.site.status.value,
            "declared_risk": self.site.declared_risk.value,
            "volume_m3": self.site.volume_m3,
            "distance_to_river_m": self.site.distance_to_river_m,
            "synthetic": self.site.synthetic,
            "in_basin": self.site.in_basin,
            "area_m2": self.site.area_m2,
            "data_quality": list(self.site.data_quality),
            "rank": self.rank,
            "risk": self.score.to_dict(),
            "static_risk": self.static_risk,
            "exposure": None,
            "water_intakes": [],
        }
        if self.exposure is not None:
            data["exposure"] = {
                "value": self.exposure.value,
                "settlements": [c.to_dict() for c in self.exposure.contributions]
                if include_exposure_detail
                else [],
            }
            data["water_intakes"] = [i.to_dict() for i in self.exposure.intakes]
        return data


def _exposure_for(
    site: TailingsSite,
    settlements: Sequence[Settlement],
    config: ExposureConfig,
    intakes: Sequence[WaterIntake] = (),
) -> ExposureResult | None:
    if site.flow_path is None:
        return None
    return downstream_exposure(site.flow_path, settlements, config, intakes)


def site_static_risk(
    site: TailingsSite,
    settlements: Sequence[Settlement],
    risk_config: RiskConfig | None = None,
    exposure_config: ExposureConfig | None = None,
    intakes: Sequence[WaterIntake] = (),
) -> float:
    """Susceptibility x TailingsVulnerability x DownstreamExposure (no time-varying trigger)."""
    rcfg = risk_config or RiskConfig()
    exposure = _exposure_for(site, settlements, exposure_config or ExposureConfig(), intakes)
    return compute_static_risk(
        susceptibility=site.susceptibility,
        vulnerability=tailings_vulnerability(site, rcfg),
        exposure=None if exposure is None else exposure.value,
        config=rcfg,
    )


def rank_sites(
    repository: SiteRepositoryPort,
    hazard_context: HazardContext,
    risk_config: RiskConfig | None = None,
    exposure_config: ExposureConfig | None = None,
    intakes: Sequence[WaterIntake] = (),
    basin_only: bool = False,
) -> list[RankedSite]:
    rcfg = risk_config or RiskConfig()
    ecfg = exposure_config or ExposureConfig()
    settlements = list(repository.list_settlements())
    trigger = trigger_factor(hazard_context, rcfg)

    scored: list[tuple[TailingsSite, RiskScore, float, float, ExposureResult | None]] = []
    for site in repository.list_sites():
        if basin_only and not site.in_basin:
            continue
        exposure = _exposure_for(site, settlements, ecfg, intakes)
        exposure_value = None if exposure is None else exposure.value
        vulnerability = tailings_vulnerability(site, rcfg)
        score = compute_risk_score(
            trigger=trigger,
            susceptibility=site.susceptibility,
            vulnerability=vulnerability,
            exposure=exposure_value,
            config=rcfg,
        )
        static = compute_static_risk(
            susceptibility=site.susceptibility,
            vulnerability=vulnerability,
            exposure=exposure_value,
            config=rcfg,
        )
        scored.append((site, score, static, vulnerability, exposure))

    scored.sort(key=lambda row: (-row[1].value, row[0].id))
    return [
        RankedSite(site=site, rank=i, score=score, static_risk=static, vulnerability=vuln, exposure=exposure)
        for i, (site, score, static, vuln, exposure) in enumerate(scored, start=1)
    ]

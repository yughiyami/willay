"""Loads the bundled synthetic JSON fixtures into domain objects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from willay.application.run_backtest import BacktestDataset, DistrictRecord, EvacuationEvent
from willay.domain.entities import (
    EnfenMagnitude,
    FlowPath,
    HazardContext,
    PathPoint,
    RainfallAnomaly,
    RiskLevel,
    SenamhiLevel,
    Settlement,
    SiteStatus,
    TailingsSite,
)
from willay.domain.geo import haversine_m

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"
RIMAC_FIXTURE = FIXTURES_DIR / "rimac_synthetic.json"
BACKTEST_FIXTURE = FIXTURES_DIR / "backtest_synthetic.json"


@dataclass(frozen=True)
class RimacFixture:
    synthetic: bool
    disclaimer: str
    region: str
    official_source: str
    hazard_scenario: HazardContext
    sites: tuple[TailingsSite, ...]
    settlements: tuple[Settlement, ...]


def _read_synthetic_json(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("synthetic") is not True:
        raise ValueError(f"{path} is not flagged synthetic: bundled fixtures must say so explicitly")
    return data


def _build_flow_path(start_lon: float, start_lat: float, river: list[list[float]], start_index: int) -> FlowPath:
    coords = [(start_lon, start_lat)] + [(lon, lat) for lon, lat in river[start_index:]]
    points: list[PathPoint] = []
    cumulative = 0.0
    for i, (lon, lat) in enumerate(coords):
        if i:
            cumulative += haversine_m(coords[i - 1][0], coords[i - 1][1], lon, lat)
        points.append(PathPoint(lon, lat, cumulative))
    return FlowPath(points=tuple(points))


def load_rimac_fixture(path: Path | str = RIMAC_FIXTURE) -> RimacFixture:
    data = _read_synthetic_json(Path(path))
    scenario = data.get("hazard_scenario", {})
    hazard = HazardContext(
        enfen_magnitude=EnfenMagnitude(scenario.get("enfen_magnitude", "neutral")),
        seasonal_rainfall_anomaly=RainfallAnomaly(scenario.get("seasonal_rainfall_anomaly", "normal")),
        senamhi_level=SenamhiLevel(scenario.get("senamhi_level", "green")),
        forecast_rain_72h_mm=float(scenario.get("forecast_rain_72h_mm", 0.0)),
        observed_rain_24h_mm=float(scenario.get("observed_rain_24h_mm", 0.0)),
    )
    rivers = data.get("rivers", {})
    sites = tuple(
        TailingsSite(
            id=s["id"],
            name=s["name"],
            lon=s["lon"],
            lat=s["lat"],
            volume_m3=s["volume_m3"],
            status=SiteStatus(s["status"]),
            declared_risk=RiskLevel(s["declared_risk"]),
            distance_to_river_m=s["distance_to_river_m"],
            susceptibility=s["susceptibility"],
            district=s.get("district", ""),
            province=s.get("province", ""),
            synthetic=True,
            flow_path=_build_flow_path(s["lon"], s["lat"], rivers[s["river"]], s["river_start_index"]),
        )
        for s in data["sites"]
    )
    settlements = tuple(
        Settlement(
            id=s["id"],
            name=s["name"],
            lon=s["lon"],
            lat=s["lat"],
            population=s["population"],
            households=s["households"],
            has_water_intake=s.get("has_water_intake", False),
            safe_point=s.get("safe_point"),
        )
        for s in data["settlements"]
    )
    return RimacFixture(
        synthetic=True,
        disclaimer=data.get("disclaimer", ""),
        region=data.get("region", ""),
        official_source=data.get("official_source", ""),
        hazard_scenario=hazard,
        sites=sites,
        settlements=settlements,
    )


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def load_backtest_fixture(path: Path | str = BACKTEST_FIXTURE) -> BacktestDataset:
    data = _read_synthetic_json(Path(path))
    districts = tuple(
        DistrictRecord(
            district=d["district"],
            score=d["score"],
            emergency=d["emergency"],
            first_preparation_alert=_parse_date(d.get("first_preparation_alert")),
            emergency_date=_parse_date(d.get("emergency_date")),
        )
        for d in data["districts"]
    )
    events = tuple(
        EvacuationEvent(e["district"], e["alerted"], e["emergency_followed"]) for e in data["evacuation_events"]
    )
    return BacktestDataset(districts=districts, evacuation_events=events, synthetic=True)

"""Composition of the API dependencies: bundled synthetic fixtures or the real data folder.

WILLAY_DATA_MODE=real (default) reads WILLAY_DATA_DIR (default ../willay-data);
WILLAY_DATA_MODE=synthetic uses the invented fixtures bundled in backend/fixtures (used by tests).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from willay.adapters.data_dir import DataDir, resolve_data_dir
from willay.adapters.fixture_loader import load_backtest_fixture, load_rimac_fixture
from willay.adapters.geodata.emergencies import (
    TARGET_HAZARDS,
    filter_emergencies,
    first_emergency_by_district,
    load_indeci_csv,
)
from willay.adapters.geodata.loader import load_real_world
from willay.adapters.geodata.timeseries import load_enfen_csv
from willay.adapters.in_memory import InMemorySiteRepository
from willay.adapters.reviewed_templates import FileReviewedTemplates
from willay.application.district_backtest import STANDARD_EVENTS
from willay.application.run_backtest import BacktestDataset
from willay.domain.district_score import DEFAULT_WEIGHTS, DistrictFeatures, score_districts
from willay.domain.entities import HazardContext, RainfallAnomaly
from willay.domain.exposure import ExposureConfig
from willay.ports.llm import LLMPort
from willay.ports.site_repository import SiteRepositoryPort

ENV_MODE = "WILLAY_DATA_MODE"
ENV_ANOMALY = "WILLAY_SEASONAL_ANOMALY"
ENV_TEMPLATES_DIR = "WILLAY_REVIEWED_TEMPLATES_DIR"
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "reviewed"
REAL_DISCLAIMER = (
    "Real open data (INGEMMET, ANA, OSM, Copernicus DEM). Tailings risk attributes are sparse, water intakes are "
    "approximate OpenStreetMap locations and flow paths come from a 30 m DEM: verify before any operational use. "
    "Willay Relaves complements the official SENAMHI, INDECI and ENFEN alerts and never replaces them."
)


@dataclass
class AppDependencies:
    repository: SiteRepositoryPort
    hazard_scenario: HazardContext
    official_source: str
    backtest_dataset: BacktestDataset | None
    synthetic: bool
    disclaimer: str
    llm: LLMPort | None = None
    data_mode: str = "synthetic"
    basin_only: bool = False
    exposure_config: ExposureConfig | None = None
    flow_path_features: Mapping[str, Mapping] = field(default_factory=dict)
    site_static_inputs: Mapping[str, Mapping] = field(default_factory=dict)
    pasivos: Sequence = ()
    district_collection: Mapping | None = None
    backtest_results: Mapping | None = None
    reviewed_templates: object | None = None


def reviewed_templates_from_env(environ: Mapping[str, str] | None = None) -> FileReviewedTemplates:
    env = os.environ if environ is None else environ
    return FileReviewedTemplates(env.get(ENV_TEMPLATES_DIR) or DEFAULT_TEMPLATES_DIR)


def synthetic_dependencies() -> AppDependencies:
    fixture = load_rimac_fixture()
    return AppDependencies(
        repository=InMemorySiteRepository(fixture.sites, fixture.settlements),
        hazard_scenario=fixture.hazard_scenario,
        official_source=fixture.official_source,
        backtest_dataset=load_backtest_fixture(),
        synthetic=fixture.synthetic,
        disclaimer=fixture.disclaimer,
        reviewed_templates=reviewed_templates_from_env(),
    )


def _current_hazard(bulletins, anomaly: RainfallAnomaly) -> tuple[HazardContext, str]:
    if not bulletins:
        return HazardContext(seasonal_rainfall_anomaly=anomaly), "no ENFEN communique available"
    latest = max(bulletins, key=lambda b: b.date)
    context = HazardContext(enfen_magnitude=latest.magnitude, seasonal_rainfall_anomaly=anomaly)
    return context, f"ENFEN Comunicado Oficial N°{latest.number} ({latest.date.isoformat()})"


def _district_collection(world, data: DataDir) -> Mapping | None:
    features_path = data.derived("district_features.json")
    if not features_path.exists():
        return None
    district_features = json.loads(features_path.read_text(encoding="utf-8"))["districts"]
    polygons_path = data.derived("district_polygons.geojson")
    geometry = {}
    if polygons_path.exists():
        for f in json.loads(polygons_path.read_text(encoding="utf-8"))["features"]:
            geometry[f["properties"]["ubigeo"]] = f["geometry"]

    scores = {
        s.ubigeo: s
        for s in score_districts(
            [
                DistrictFeatures(
                    ubigeo=ubigeo,
                    name=f["name"],
                    mm_high_share=f["mm_high_share"],
                    mean_slope_deg=f["mean_slope_deg"],
                    exposed_population=f["exposed_population"].get("all"),
                    tailings_upstream=f["tailings_upstream"],
                )
                for ubigeo, f in district_features.items()
            ]
        )
    }
    cenepred = {d.ubigeo: d for d in world.cenepred}

    indeci_path = data.processed("indeci_emergencias_distritos_bbox_2003_2023.csv")
    windows: dict[str, dict[str, date]] = {}
    counts: dict[str, dict[str, int]] = {}
    if indeci_path.exists():
        emergencies = load_indeci_csv(indeci_path)
        for key, event in STANDARD_EVENTS.items():
            inside = filter_emergencies(emergencies, TARGET_HAZARDS, event.label_start, event.label_end)
            windows[key] = first_emergency_by_district(inside)
            counts[key] = {}
            for e in inside:
                counts[key][e.ubigeo] = counts[key].get(e.ubigeo, 0) + 1

    features = []
    for ubigeo, feats in district_features.items():
        score = scores[ubigeo]
        c = cenepred.get(ubigeo)
        features.append(
            {
                "type": "Feature",
                "geometry": geometry.get(ubigeo),
                "properties": {
                    "ubigeo": ubigeo,
                    "name": feats["name"],
                    "static_score": score.value,
                    "components": {k: v for k, v in score.components.items()},
                    "missing_components": list(score.missing),
                    "features": {k: v for k, v in feats.items() if k != "tailings_upstream_ids"},
                    "cenepred": None
                    if c is None
                    else {
                        "baseline_only": True,
                        "scenario": "El Nino 2027 (SENAMHI IT N°07-2026)",
                        "flood_risk_value": c.flood_risk_value,
                        "flood_risk_level": c.flood_risk_level,
                        "mm_risk_value": c.mm_risk_value,
                        "mm_risk_level": c.mm_risk_level,
                        "population_2017": c.population_2017,
                    },
                    "emergencies": {
                        key: {
                            "count": counts.get(key, {}).get(ubigeo, 0),
                            "first_date": windows[key][ubigeo].isoformat() if ubigeo in windows.get(key, {}) else None,
                        }
                        for key in STANDARD_EVENTS
                    },
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "scoring": {
            "weights": dict(DEFAULT_WEIGHTS),
            "method": "percentile ranks of INGEMMET susceptibility share, mean slope, exposed population and "
            "upstream tailings, combined with fixed a-priori weights; no outcome or CENEPRED input",
            "emergency_hazards": list(TARGET_HAZARDS),
        },
        "features": features,
    }


def real_dependencies(data: DataDir | None = None, environ: Mapping[str, str] | None = None) -> AppDependencies:
    env = os.environ if environ is None else environ
    data = data or DataDir(resolve_data_dir(env))
    world = load_real_world(data)
    anomaly = RainfallAnomaly(env.get(ENV_ANOMALY, RainfallAnomaly.NORMAL.value))

    enfen_path = data.processed("enfen_timeline.csv")
    bulletins = load_enfen_csv(enfen_path) if enfen_path.exists() else []
    hazard, source = _current_hazard(bulletins, anomaly)

    results_path = data.derived("backtest_results.json")
    backtest_results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else None

    return AppDependencies(
        repository=InMemorySiteRepository(world.sites, world.settlements, world.intakes),
        hazard_scenario=hazard,
        official_source=source,
        backtest_dataset=None,
        synthetic=False,
        disclaimer=REAL_DISCLAIMER,
        data_mode="real",
        basin_only=True,
        # Flow paths are capped at 80 km, so exposure is evaluated over the whole path.
        exposure_config=ExposureConfig(cutoff_m=80_000.0),
        flow_path_features=world.flow_path_geojson,
        site_static_inputs={
            site_id: {
                **features,
                "flow_path_stop_reason": world.flow_path_meta.get(site_id, {}).get("stop_reason"),
                "flow_path_length_m": world.flow_path_meta.get(site_id, {}).get("length_m"),
            }
            for site_id, features in world.site_features.items()
        },
        pasivos=world.pasivos,
        district_collection=_district_collection(world, data),
        backtest_results=backtest_results,
        reviewed_templates=reviewed_templates_from_env(env),
    )


def default_dependencies() -> AppDependencies:
    mode = os.environ.get(ENV_MODE, "real").strip().lower()
    if mode == "synthetic":
        return synthetic_dependencies()
    if mode == "real":
        return real_dependencies()
    raise ValueError(f"{ENV_MODE} must be 'real' or 'synthetic', got '{mode}'")


__all__ = [
    "AppDependencies",
    "default_dependencies",
    "real_dependencies",
    "synthetic_dependencies",
]

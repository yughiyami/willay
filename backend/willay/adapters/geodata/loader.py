"""Composes the real-data world (sites, settlements, intakes, baselines) from WILLAY_DATA_DIR."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping

from willay.adapters.data_dir import DataDir
from willay.adapters.geodata.hazards import CenepredDistrict, load_cenepred_csv
from willay.adapters.geodata.intakes import load_osm_intakes
from willay.adapters.geodata.settlements import load_settlements_csv
from willay.adapters.geodata.tailings import PasivoRecord, TailingsRecord, load_pasivos_csv, load_tailings_csv
from willay.domain.entities import FlowPath, PathPoint, Settlement, TailingsSite, WaterIntake

NEUTRAL_SUSCEPTIBILITY = 0.5
# Without a distance-to-river measurement a site is assumed far from the river (low weight).
DEFAULT_DISTANCE_TO_RIVER_M = 5_000.0

BUILD_HINT = (
    "run scripts/build_flowpaths.py, scripts/build_site_features.py and scripts/build_district_features.py"
)


def susceptibility_from_class(klass: int) -> float:
    """INGEMMET class 1 (very low) .. 5 (very high) to the middle of five equal bins of [0, 1]."""
    return (klass - 0.5) / 5.0


def build_flow_path(feature: Mapping) -> FlowPath:
    coords = feature["geometry"]["coordinates"]
    cumulative = feature["properties"]["cumulative_distance_m"]
    if len(coords) != len(cumulative):
        raise ValueError("flow path vertices and cumulative distances differ in length")
    return FlowPath(points=tuple(PathPoint(lon, lat, dist) for (lon, lat), dist in zip(coords, cumulative)))


def _build_site(
    record: TailingsRecord, features: Mapping | None, flow_path: FlowPath | None
) -> TailingsSite:
    flags = list(record.data_quality)
    susceptibility = NEUTRAL_SUSCEPTIBILITY
    distance = DEFAULT_DISTANCE_TO_RIVER_M
    if features is None:
        flags.append("no_site_features")
    else:
        klass = features.get("susceptibility_class")
        if klass is None:
            flags.append("susceptibility_unsampled")
        else:
            susceptibility = susceptibility_from_class(int(klass))
            if features.get("susceptibility_method") not in (None, "point_in_polygon"):
                flags.append("susceptibility_from_nearest_polygon")
        if features.get("distance_to_river_m") is not None:
            distance = float(features["distance_to_river_m"])
    if flow_path is None:
        flags.append("no_flow_path")
    return TailingsSite(
        id=record.id,
        name=record.name,
        lon=record.lon,
        lat=record.lat,
        volume_m3=record.volume_m3 or 0.0,
        status=record.status,
        declared_risk=record.declared_risk,
        distance_to_river_m=distance,
        susceptibility=susceptibility,
        district=record.district,
        province=record.province,
        synthetic=False,
        flow_path=flow_path,
        data_quality=tuple(flags),
        in_basin=record.in_basin,
        area_m2=record.area_m2,
    )


@dataclass(frozen=True)
class RealWorld:
    data_dir: DataDir
    sites: tuple[TailingsSite, ...]
    settlements: tuple[Settlement, ...]
    intakes: tuple[WaterIntake, ...]
    pasivos: tuple[PasivoRecord, ...]
    cenepred: tuple[CenepredDistrict, ...]
    site_features: Mapping[str, Mapping]
    flow_path_meta: Mapping[str, Mapping]
    flow_path_geojson: Mapping[str, Mapping] = field(default_factory=dict)


def load_real_world(data: DataDir) -> RealWorld:
    required = [
        data.processed("relaves_inventario_ingemmet_bbox.csv"),
        data.processed("pasivos_ambientales_minem_bbox.csv"),
        data.processed("ccpp_cuenca_rimac_pob.csv"),
        data.processed("cenepred_er_fen2027_distritos_bbox.csv"),
        data.raw("osm", "overpass_water_infrastructure_bbox.json"),
        data.derived("flowpaths.geojson"),
        data.derived("site_features.json"),
    ]
    data.require(*required)

    flow_collection = json.loads(data.derived("flowpaths.geojson").read_text(encoding="utf-8"))
    flow_features = {f["properties"]["site_id"]: f for f in flow_collection["features"]}
    flow_paths = {site_id: build_flow_path(f) for site_id, f in flow_features.items()}
    flow_meta = {
        site_id: {k: v for k, v in f["properties"].items() if k != "cumulative_distance_m"}
        for site_id, f in flow_features.items()
    }
    site_features = json.loads(data.derived("site_features.json").read_text(encoding="utf-8"))["sites"]

    records = load_tailings_csv(data.processed("relaves_inventario_ingemmet_bbox.csv"))
    sites = tuple(_build_site(r, site_features.get(r.id), flow_paths.get(r.id)) for r in records)
    return RealWorld(
        data_dir=data,
        sites=sites,
        settlements=tuple(load_settlements_csv(data.processed("ccpp_cuenca_rimac_pob.csv"))),
        intakes=tuple(load_osm_intakes(data.raw("osm", "overpass_water_infrastructure_bbox.json"))),
        pasivos=tuple(load_pasivos_csv(data.processed("pasivos_ambientales_minem_bbox.csv"))),
        cenepred=tuple(load_cenepred_csv(data.processed("cenepred_er_fen2027_distritos_bbox.csv"))),
        site_features=site_features,
        flow_path_meta=flow_meta,
        flow_path_geojson=flow_features,
    )


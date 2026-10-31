"""Offline build: static, label-independent features per district of the study bbox.

Inputs (all read-only): INGEMMET susceptibility (area per class and district), Copernicus DEM (slope),
ANA settlements (Pob_rev), ANA critical points, IGN rivers, the D8 flow paths and the tailings CSV.
District polygons come from the CENEPRED district layer (geometry and name only; none of its
risk values is used here).
Writes <data>/derived/district_features.json

Run:  .venv/Scripts/python -I scripts/build_district_features.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import shapely  # noqa: E402
import tifffile  # noqa: E402
from shapely.geometry import LineString, Point, shape  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.geodata.hazards import parse_critical_points, summarize_susceptibility_by_district  # noqa: E402
from willay.adapters.geodata.settlements import load_settlements_csv  # noqa: E402
from willay.adapters.geodata.tailings import load_tailings_csv  # noqa: E402
from willay.adapters.geodata.text import pad_ubigeo  # noqa: E402

LAT0 = -11.9
KX, KY = 111_320.0 * math.cos(math.radians(LAT0)), 110_574.0
RIVER_BUFFER_M = 500.0
CRITICAL_POINT_BUFFER_M = 1_000.0
# ANA critical points are designated year by year (2016-2025). To limit leakage, the exposure feature
# for an event year only uses points designated BEFORE that year.
EVENT_YEARS = (2017, 2023)


def main() -> None:
    data = DataDir(resolve_data_dir())
    paths = {
        "susc": data.raw("ingemmet", "susceptibilidad_mov_masa_distrital_bbox.geojson"),
        "districts": data.raw("cenepred", "er_fen2027_riesgo_movmasa_distritos_bbox.geojson"),
        "dem": data.processed("dem_glo30_rimac_bbox.tif"),
        "dem_meta": data.processed("dem_glo30_rimac_bbox.meta.json"),
        "ccpp": data.processed("ccpp_cuenca_rimac_pob.csv"),
        "critical": data.raw("ana", "puntos_criticos_2016_2024_bbox.geojson"),
        "rivers": data.raw("ana", "rios_quebradas_ign_bbox.geojson"),
        "flowpaths": data.derived("flowpaths.geojson"),
        "tailings": data.processed("relaves_inventario_ingemmet_bbox.csv"),
    }
    data.require(*paths.values())

    districts = {}
    for f in json.loads(paths["districts"].read_text(encoding="utf-8"))["features"]:
        ubigeo = pad_ubigeo(f["properties"]["ubigeo"])
        districts[ubigeo] = (f["properties"]["nombdist"], shape(f["geometry"]))
    print(f"{len(districts)} districts")

    # 1. share of high / very-high susceptibility area (attribute aggregation by district)
    print("summarising susceptibility...")
    susc = summarize_susceptibility_by_district(
        json.loads(paths["susc"].read_text(encoding="utf-8"))["features"]
    )

    # 2. mean slope over land cells of each district
    meta = json.loads(paths["dem_meta"].read_text(encoding="utf-8"))
    ox, oy = meta["origin_ul"]
    res = meta["res"][0]
    dem = tifffile.imread(paths["dem"]).astype(np.float32)
    land = dem > 0.5
    dzdy, dzdx = np.gradient(np.where(dem < -9000, np.nan, dem), res * KY, res * KX)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    rows, cols = dem.shape

    def mean_slope(poly) -> float | None:
        minx, miny, maxx, maxy = poly.bounds
        c0, c1 = max(0, int((minx - ox) / res)), min(cols, int(math.ceil((maxx - ox) / res)))
        r0, r1 = max(0, int((oy - maxy) / res)), min(rows, int(math.ceil((oy - miny) / res)))
        if c1 <= c0 or r1 <= r0:
            return None
        xs = ox + (np.arange(c0, c1) + 0.5) * res
        ys = oy - (np.arange(r0, r1) + 0.5) * res
        gx, gy = np.meshgrid(xs, ys)
        inside = shapely.contains_xy(poly, gx, gy)
        window = slope[r0:r1, c0:c1]
        ok = inside & land[r0:r1, c0:c1] & np.isfinite(window)
        return float(window[ok].mean()) if ok.any() else None

    # 3. population (ANA Pob_rev) of settlements near rivers or ANA critical points
    rivers = unary_union(
        [shape(f["geometry"]) for f in json.loads(paths["rivers"].read_text(encoding="utf-8"))["features"]]
    )
    critical = parse_critical_points(json.loads(paths["critical"].read_text(encoding="utf-8")))

    def metres_to(geom, lon: float, lat: float) -> float:
        p = Point(lon, lat)
        q = geom.interpolate(geom.project(p)) if geom.geom_type != "Point" else geom
        return math.hypot((q.x - lon) * KX, (q.y - lat) * KY)

    settlements = load_settlements_csv(paths["ccpp"])
    exposed_pop: dict[int, dict[str, int]] = {year: {} for year in EVENT_YEARS}
    for s in settlements:
        near_river = metres_to(rivers, s.lon, s.lat) <= RIVER_BUFFER_M
        for year in EVENT_YEARS:
            near_cp = any(
                cp.period is not None
                and cp.period < year
                and math.hypot((cp.lon - s.lon) * KX, (cp.lat - s.lat) * KY) <= CRITICAL_POINT_BUFFER_M
                for cp in critical
            )
            if near_river or near_cp:
                exposed_pop[year][s.ubigeo] = exposed_pop[year].get(s.ubigeo, 0) + s.population

    # 4. tailings upstream: sites whose flow path crosses the district
    sites = {s.id: s for s in load_tailings_csv(paths["tailings"])}
    flow_lines = {}
    for f in json.loads(paths["flowpaths"].read_text(encoding="utf-8"))["features"]:
        flow_lines[f["properties"]["site_id"]] = LineString(f["geometry"]["coordinates"])

    out = {}
    for ubigeo, (name, poly) in sorted(districts.items()):
        upstream = [sid for sid, line in flow_lines.items() if line.intersects(poly)]
        summary = susc.get(ubigeo)
        out[ubigeo] = {
            "name": name,
            "mm_high_share": round(summary.high_share, 4) if summary else None,
            "mm_total_area_km2": round(summary.total_area_km2, 2) if summary else None,
            "mean_slope_deg": None if (m := mean_slope(poly)) is None else round(m, 2),
            "exposed_population": {str(y): exposed_pop[y].get(ubigeo, 0) for y in EVENT_YEARS},
            "tailings_upstream": len(upstream),
            "tailings_upstream_ids": upstream,
            "tailings_upstream_volume_m3": round(sum(sites[s].volume_m3 or 0.0 for s in upstream), 1),
        }
        o = out[ubigeo]
        print(f"{ubigeo} {name[:26]:26s} high={o['mm_high_share']} slope={o['mean_slope_deg']} "
              f"exp_pop={o['exposed_population']['2017']:8d}/{o['exposed_population']['2023']:8d} tailings={o['tailings_upstream']}")

    payload = {
        "definition": {
            "mm_high_share": "area share of INGEMMET susceptibility classes 4-5 (alta, muy alta), by CD_DIST",
            "mean_slope_deg": "mean slope of land cells (>0.5 m) from Copernicus GLO-30",
            "exposed_population": f"per event year: sum of ANA Pob_rev of settlements within {RIVER_BUFFER_M:.0f} m of "
            f"an IGN river line or {CRITICAL_POINT_BUFFER_M:.0f} m of an ANA critical point designated before that year",
            "tailings_upstream": "INGEMMET tailings sites whose D8 flow path (80 km cap) crosses the district",
        },
        "districts": out,
    }
    target = data.write_derived("district_features.json", json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()

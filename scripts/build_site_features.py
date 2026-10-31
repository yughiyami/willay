"""Offline build: static features per tailings site (INGEMMET susceptibility sample, distance to river).

Reads  <data>/raw/ingemmet/susceptibilidad_mov_masa_distrital_bbox.geojson (117 MB), the IGN rivers and
       the tailings CSV.
Writes <data>/derived/site_features.json

Run:  .venv/Scripts/python -I scripts/build_site_features.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from shapely import STRtree  # noqa: E402
from shapely.geometry import Point, shape  # noqa: E402

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.geodata.hazards import sample_susceptibility_class  # noqa: E402
from willay.adapters.geodata.tailings import load_tailings_csv  # noqa: E402

LABELS = {1: "Muy baja", 2: "Baja", 3: "Media", 4: "Alta", 5: "Muy alta"}
NEAREST_POLYGON_MAX_M = 200.0
LAT0 = -11.9
KX, KY = 111_320.0 * math.cos(math.radians(LAT0)), 110_574.0


def meters(dx_deg: float, dy_deg: float) -> float:
    return math.hypot(dx_deg * KX, dy_deg * KY)


def main() -> None:
    data = DataDir(resolve_data_dir())
    susc_path = data.raw("ingemmet", "susceptibilidad_mov_masa_distrital_bbox.geojson")
    rivers_path = data.raw("ana", "rios_quebradas_ign_bbox.geojson")
    csv_path = data.processed("relaves_inventario_ingemmet_bbox.csv")
    data.require(susc_path, rivers_path, csv_path)

    print("loading susceptibility polygons (this takes a few seconds)...")
    features = [
        f
        for f in json.loads(susc_path.read_text(encoding="utf-8"))["features"]
        if isinstance(f["properties"].get("GRIDCODE"), (int, float)) and f["properties"]["GRIDCODE"] >= 1
    ]
    geoms = [shape(f["geometry"]) for f in features]
    susc_tree = STRtree(geoms)

    rivers = json.loads(rivers_path.read_text(encoding="utf-8"))["features"]
    river_geoms = [shape(f["geometry"]) for f in rivers]
    river_names = [f["properties"].get("NOMBRE_COMPLETO") or "unnamed" for f in rivers]
    river_tree = STRtree(river_geoms)

    out: dict[str, dict] = {}
    for site in load_tailings_csv(csv_path):
        point = Point(site.lon, site.lat)
        # Candidate polygons whose bbox contains the point, then exact point-in-polygon in the adapter.
        candidates = [features[i] for i in susc_tree.query(point, predicate="intersects")]
        klass = sample_susceptibility_class(site.lon, site.lat, candidates)
        method = "point_in_polygon"
        if klass is None:
            idx = int(susc_tree.nearest(point))
            g = geoms[idx]
            q = g.interpolate(g.project(point))
            if meters(q.x - site.lon, q.y - site.lat) <= NEAREST_POLYGON_MAX_M:
                klass, method = int(features[idx]["properties"]["GRIDCODE"]), "nearest_polygon_within_200m"
            else:
                method = "none"
        ridx = int(river_tree.nearest(point))
        rg = river_geoms[ridx]
        rq = rg.interpolate(rg.project(point))
        out[site.id] = {
            "susceptibility_class": klass,
            "susceptibility_label": LABELS.get(klass) if klass else None,
            "susceptibility_method": method,
            "distance_to_river_m": round(meters(rq.x - site.lon, rq.y - site.lat), 1),
            "nearest_river": river_names[ridx],
        }
        print(f"{site.id:16s} class={klass} ({method}) river={out[site.id]['nearest_river']} "
              f"{out[site.id]['distance_to_river_m']} m")

    payload = {
        "sources": {
            "susceptibility": "INGEMMET susceptibility to mass movements (regional model), sampled at the site location",
            "rivers": "IGN rivers and quebradas (via ANA); includes small quebradas",
        },
        "sites": out,
    }
    target = data.write_derived("site_features.json", json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()

"""Offline build: one deterministic safe-point PROPOSAL per settlement.

Reads  <data>/processed/dem_glo30_rimac_bbox.tif (+ .meta.json), <data>/processed/ccpp_cuenca_rimac_pob.csv and
       <data>/derived/flowpaths.geojson (build it first with scripts/build_flowpaths.py).
Writes <data>/derived/safe_points.geojson.

Rules (domain/safe_point.py): the nearest DEM cell that is at least H m above the nearest flow-path cell, at
least D m from any flow path, within R km in a straight line and with a slope below S degrees.
Override with --height, --distance, --radius, --slope. Every point is a proposal to validate with the municipality.

Run:  .venv/Scripts/python -I scripts/build_safe_points.py
Contains: Copernicus DEM, produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and
(c) Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import tifffile  # noqa: E402

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.dem_window import extract_window  # noqa: E402
from willay.adapters.geodata.settlements import load_settlements_csv  # noqa: E402
from willay.adapters.safe_points_file import FILE_NAME, build_collection  # noqa: E402
from willay.domain.safe_point import SafePointConfig, select_safe_point  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    defaults = SafePointConfig()
    parser.add_argument("--height", type=float, default=defaults.min_height_above_flow_m, help="min metres above the river")
    parser.add_argument("--distance", type=float, default=defaults.min_distance_from_flow_m, help="min metres from any flow path")
    parser.add_argument("--radius", type=float, default=defaults.max_distance_m, help="max straight-line metres from the settlement")
    parser.add_argument("--slope", type=float, default=defaults.max_slope_deg, help="slope must be below this many degrees")
    args = parser.parse_args()
    config = SafePointConfig(args.height, args.distance, args.radius, args.slope)

    data = DataDir(resolve_data_dir())
    dem_path = data.processed("dem_glo30_rimac_bbox.tif")
    meta_path = data.processed("dem_glo30_rimac_bbox.meta.json")
    csv_path = data.processed("ccpp_cuenca_rimac_pob.csv")
    flow_path = data.derived("flowpaths.geojson")
    data.require(dem_path, meta_path, csv_path, flow_path)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    origin = (meta["origin_ul"][0], meta["origin_ul"][1])
    res = (meta["res"][0], meta["res"][1])
    dem = tifffile.imread(dem_path)
    flow_lines = [
        [(x, y) for x, y in feature["geometry"]["coordinates"]]
        for feature in json.loads(flow_path.read_text(encoding="utf-8"))["features"]
    ]
    print(f"DEM {dem.shape[0]}x{dem.shape[1]}, {len(flow_lines)} flow paths, rules {config}")

    # The window must hold every candidate (R) plus every flow path that can disqualify it (D) and some margin.
    window_radius = config.max_distance_m + max(config.min_distance_from_flow_m, 0.0) + 200.0
    entries = []
    for settlement in load_settlements_csv(csv_path):
        grid = extract_window(dem, origin, res, settlement.lon, settlement.lat, window_radius)
        point = None if grid is None else select_safe_point(settlement.lon, settlement.lat, grid, flow_lines, config)
        status = "no point" if point is None else f"{point.distance_m:6.0f} m @ {point.bearing_deg:3.0f} deg, +{point.height_above_flow_m:.0f} m"
        print(f"{settlement.id:16s} {settlement.name[:28]:28s} {status}")
        entries.append((settlement.id, settlement.name, settlement.lon, settlement.lat, point))

    found = sum(1 for e in entries if e[4] is not None)
    target = data.write_derived(FILE_NAME, json.dumps(build_collection(entries, config), separators=(",", ":")))
    print(f"wrote {target}: {found}/{len(entries)} settlements have a proposed safe point")


if __name__ == "__main__":
    main()

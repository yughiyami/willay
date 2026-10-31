"""Offline build: D8 downstream flow paths from every tailings site.

Reads  <data>/processed/dem_glo30_rimac_bbox.tif  (Copernicus GLO-30, clipped) and the tailings CSV.
Writes <data>/derived/flowpaths.geojson           (one LineString per site, cumulative distance per vertex).

Run:  .venv/Scripts/python -I scripts/build_flowpaths.py
Contains: Copernicus DEM, produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and
(c) Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import tifffile  # noqa: E402

from willay.adapters.d8 import D8Config, simplify_trace, trace_d8  # noqa: E402
from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.geodata.tailings import load_tailings_csv  # noqa: E402

SIMPLIFY_TOLERANCE_M = 20.0
NODATA = -9000.0  # DEM nodata is -9999


def main() -> None:
    data = DataDir(resolve_data_dir())
    dem_path = data.processed("dem_glo30_rimac_bbox.tif")
    meta_path = data.processed("dem_glo30_rimac_bbox.meta.json")
    csv_path = data.processed("relaves_inventario_ingemmet_bbox.csv")
    data.require(dem_path, meta_path, csv_path)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ox, oy = meta["origin_ul"]
    res_x, res_y = meta["res"]
    dem = tifffile.imread(dem_path)
    rows, cols = dem.shape
    print(f"DEM {rows}x{cols}, origin ({ox:.5f}, {oy:.5f}), res {res_x:.8f}")

    def elevation(r: int, c: int):
        value = dem[r, c]
        return None if value < NODATA else float(value)

    def cell_lonlat(r: int, c: int) -> tuple[float, float]:
        return ox + (c + 0.5) * res_x, oy - (r + 0.5) * res_y

    features = []
    for site in load_tailings_csv(csv_path):
        c = int((site.lon - ox) / res_x)
        r = int((oy - site.lat) / res_y)
        if not (0 <= r < rows and 0 <= c < cols) or elevation(r, c) is None:
            print(f"{site.id}: outside DEM data, skipped")
            continue
        result = trace_d8(elevation, rows, cols, (r, c), cell_lonlat, D8Config())
        coords = [cell_lonlat(*cell) for cell in result.cells]
        # Start the line at the site itself so the first vertex is the deposit, not the cell centre.
        coords[0] = (site.lon, site.lat)
        kept, cumulative = simplify_trace(coords, list(result.cumulative_m), SIMPLIFY_TOLERANCE_M)
        print(
            f"{site.id:18s} {site.name[:30]:30s} {result.cumulative_m[-1] / 1000:6.1f} km "
            f"{len(result.cells):5d}->{len(kept):4d} vertices  stop={result.stop_reason} breached={result.breached_cells}"
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in kept]},
                "properties": {
                    "site_id": site.id,
                    "name": site.name,
                    "length_m": round(result.cumulative_m[-1], 1),
                    "stop_reason": result.stop_reason,
                    "breached_cells": result.breached_cells,
                    "cumulative_distance_m": [round(v, 1) for v in cumulative],
                },
            }
        )

    collection = {
        "type": "FeatureCollection",
        "properties": {
            "method": "D8 steepest descent on Copernicus GLO-30 with minimal local pit breaching",
            "max_length_m": D8Config().max_length_m,
            "simplify_tolerance_m": SIMPLIFY_TOLERANCE_M,
            "attribution": "Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space "
            "GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved",
            "caveat": "DSM-based, 30 m: bridges, buildings and dams can bend a path; validate before operational use",
        },
        "features": features,
    }
    target = data.write_derived("flowpaths.geojson", json.dumps(collection, separators=(",", ":")))
    print(f"wrote {target} ({len(features)} paths, {target.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

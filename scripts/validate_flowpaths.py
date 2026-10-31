"""Offline check: do the D8 flow paths follow the IGN rivers and quebradas?

For every path it samples the line every 250 m and measures the distance to the nearest IGN river
line. A path is flagged when more than 25% of its length beyond the first 3 km is farther than 300 m
from any IGN line, or when a long stretch (>= 3 km) is off the network.
Writes <data>/derived/flowpaths_validation.json and prints a table.

Run:  .venv/Scripts/python -I scripts/validate_flowpaths.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from shapely import STRtree  # noqa: E402
from shapely.geometry import LineString, Point, shape  # noqa: E402

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402

SAMPLE_STEP_M = 250.0
SKIP_FIRST_M = 3_000.0
OFF_NETWORK_M = 300.0
LONG_STRETCH_M = 3_000.0
MAX_OFF_SHARE = 0.25


def main() -> None:
    data = DataDir(resolve_data_dir())
    flow_path = data.derived("flowpaths.geojson")
    rivers_path = data.raw("ana", "rios_quebradas_ign_bbox.geojson")
    data.require(flow_path, rivers_path)

    rivers = json.loads(rivers_path.read_text(encoding="utf-8"))["features"]
    geoms = [shape(f["geometry"]) for f in rivers]
    names = [f["properties"].get("NOMBRE_COMPLETO") or "unnamed" for f in rivers]
    tree = STRtree(geoms)

    lat0 = -11.9
    kx, ky = 111_320.0 * math.cos(math.radians(lat0)), 110_574.0

    def nearest(lon: float, lat: float) -> tuple[float, str]:
        p = Point(lon, lat)
        idx = tree.nearest(p)
        g = geoms[idx]
        # planar degrees -> metres with a local scale; good enough for a 300 m threshold
        q = g.interpolate(g.project(p))
        return math.hypot((q.x - lon) * kx, (q.y - lat) * ky), names[idx]

    report = []
    for feature in json.loads(flow_path.read_text(encoding="utf-8"))["features"]:
        props = feature["properties"]
        line = LineString(feature["geometry"]["coordinates"])
        cum = props["cumulative_distance_m"]
        total = cum[-1]
        samples = []
        d = SKIP_FIRST_M
        while d <= total:
            frac = d / total
            pt = line.interpolate(frac, normalized=True)  # near-uniform spacing after simplification
            dist, name = nearest(pt.x, pt.y)
            samples.append((d, dist, name))
            d += SAMPLE_STEP_M
        off = [s for s in samples if s[1] > OFF_NETWORK_M]
        longest_off = run = 0.0
        for _, dist, _ in samples:
            run = run + SAMPLE_STEP_M if dist > OFF_NETWORK_M else 0.0
            longest_off = max(longest_off, run)
        off_share = len(off) / len(samples) if samples else 0.0
        river_names: list[str] = []
        for _, dist, name in samples:
            if dist <= OFF_NETWORK_M and (not river_names or river_names[-1] != name):
                river_names.append(name)
        end_name = river_names[-1] if river_names else None
        flagged = (
            off_share > MAX_OFF_SHARE
            or longest_off >= LONG_STRETCH_M
            or (props["stop_reason"] == "max_length" and end_name is not None and "mac" not in end_name.lower())
        )
        report.append(
            {
                "site_id": props["site_id"],
                "name": props["name"],
                "length_km": round(total / 1000, 1),
                "stop_reason": props["stop_reason"],
                "off_network_share": round(off_share, 3),
                "longest_off_network_km": round(longest_off / 1000, 2),
                "rivers_followed": river_names,
                "ends_on": end_name,
                "flagged": flagged,
            }
        )

    target = data.write_derived("flowpaths_validation.json", json.dumps(report, indent=1, ensure_ascii=False))
    for row in report:
        flag = "CHECK" if row["flagged"] else "ok"
        print(
            f"{flag:5s} {row['site_id']:16s} {row['length_km']:5.1f} km off={row['off_network_share']:.2f} "
            f"longest_off={row['longest_off_network_km']:.2f} stop={row['stop_reason']:11s} "
            f"{' > '.join(row['rivers_followed'][-4:])}"
        )
    print(f"wrote {target}")


if __name__ == "__main__":
    main()

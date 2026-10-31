"""Read and write `derived/safe_points.geojson`: one proposed safe point per settlement (or none).

The file is produced offline by scripts/build_safe_points.py from the clipped Copernicus DEM and the D8 flow
paths. Every point is a PROPOSAL (computed from a 30 m DSM): it must be validated with the municipality.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from willay.domain.safe_point import SafePoint, SafePointConfig

LABEL = "propuesto — validar con la municipalidad"
FILE_NAME = "safe_points.geojson"
_METRICS = ("elevation_m", "distance_m", "bearing_deg", "height_above_flow_m", "distance_to_flow_m", "slope_deg")


def build_collection(
    entries: Iterable[tuple[str, str, float, float, SafePoint | None]], config: SafePointConfig
) -> dict:
    features = []
    for settlement_id, name, lon, lat, point in entries:
        properties: dict = {"settlement_id": settlement_id, "settlement_name": name, "found": point is not None}
        geometry = None
        if point is not None:
            properties.update({key: round(getattr(point, key), 1) for key in _METRICS})
            properties["label"] = LABEL
            geometry = {"type": "Point", "coordinates": [round(point.lon, 6), round(point.lat, 6)]}
        features.append({"type": "Feature", "geometry": geometry, "properties": properties})
    return {
        "type": "FeatureCollection",
        "properties": {
            "label": LABEL,
            "method": "nearest cell of the Copernicus GLO-30 DEM satisfying the rules below, deterministic",
            "rules": asdict(config),
            "caveat": "Proposals computed from a 30 m DSM, not surveyed locations: validate with the municipality "
            "before any operational use. Settlements without a point have none that satisfies every rule.",
            "attribution": "Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space "
            "GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved",
        },
        "features": features,
    }


def load_proposed_safe_points(path: Path | str) -> dict[str, SafePoint]:
    """settlement_id -> SafePoint for the settlements that have one. A missing or broken file yields {}."""
    try:
        collection = json.loads(Path(path).read_text(encoding="utf-8"))
        out: dict[str, SafePoint] = {}
        for feature in collection["features"]:
            props = feature["properties"]
            if not props.get("found") or feature.get("geometry") is None:
                continue
            lon, lat = feature["geometry"]["coordinates"]
            out[props["settlement_id"]] = SafePoint(lon=lon, lat=lat, **{key: float(props[key]) for key in _METRICS})
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return {}

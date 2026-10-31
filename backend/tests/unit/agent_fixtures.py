"""Shared fixtures for the operator agent tests."""

from willay.api.dependencies import synthetic_dependencies


def synthetic_toolkit_parts():
    deps = synthetic_dependencies()
    return deps.repository, deps.hazard_scenario


def _square(lon0, lat0, lon1, lat1):
    return {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


DISTRICTS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": _square(-76.6, -11.9, -76.4, -11.7),
            "properties": {"ubigeo": "150722", "name": "SAN MATEO", "static_score": 0.8, "cenepred": None, "emergencies": {}},
        },
        {
            "type": "Feature",
            "geometry": _square(-76.8, -12.0, -76.6, -11.8),
            "properties": {"ubigeo": "150103", "name": "LURIGANCHO", "static_score": 0.4, "cenepred": None, "emergencies": {}},
        },
    ],
}

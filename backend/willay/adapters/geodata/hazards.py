"""Hazard layers: INGEMMET susceptibility, ANA critical points, CENEPRED El Nino 2027 scenario.

The CENEPRED scenario is a 2027 forecast by district. It is read only as a comparison baseline
for the backtest, never as an input to our own scores.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from willay.adapters.geodata.text import clean, pad_ubigeo, to_float

HIGH_SUSCEPTIBILITY_MIN_CLASS = 4  # GRIDCODE 4 = Alta, 5 = Muy alta
# Square degrees -> km2 at the study latitude (about -11.9). Only used to give areas a unit;
# shares are scale-free, and the error across the 0.7 degree bbox is below 0.1%.
KM2_PER_DEG2 = 111.32 * 0.97851 * 110.574


# --- INGEMMET susceptibility to mass movements ---------------------------------


def _ring_contains(ring: Sequence[Sequence[float]], x: float, y: float) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _polygon_contains(rings: Sequence[Sequence[Sequence[float]]], x: float, y: float) -> bool:
    outer = rings[0]
    xs = [p[0] for p in outer]
    ys = [p[1] for p in outer]
    if not (min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys)):
        return False
    if not _ring_contains(outer, x, y):
        return False
    return not any(_ring_contains(hole, x, y) for hole in rings[1:])


def _polygons(geometry: Mapping) -> list:
    kind = geometry.get("type")
    if kind == "Polygon":
        return [geometry["coordinates"]]
    if kind == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def sample_susceptibility_class(lon: float, lat: float, features: Iterable[Mapping]) -> int | None:
    """Susceptibility class (1 very low .. 5 very high) of the polygon(s) containing the point.

    Overlapping polygons (district boundaries) resolve to the highest class. Class 0 means
    'no data' in the source and is not a valid sample.
    """
    best: int | None = None
    for feature in features:
        grid = feature.get("properties", {}).get("GRIDCODE")
        if not isinstance(grid, (int, float)) or grid < 1:
            continue
        if best is not None and grid <= best:
            continue
        geometry = feature.get("geometry") or {}
        if any(_polygon_contains(rings, lon, lat) for rings in _polygons(geometry)):
            best = int(grid)
    return best


@dataclass(frozen=True)
class DistrictSusceptibility:
    total_area_km2: float
    high_area_km2: float

    @property
    def high_share(self) -> float:
        return self.high_area_km2 / self.total_area_km2 if self.total_area_km2 > 0 else 0.0


def _polygon_area_km2(props: Mapping) -> float:
    """AREA_KM2 is rounded to integers in the INGEMMET layer (0 for most polygons): use SHAPE.AREA."""
    shape_area = to_float(props.get("SHAPE.AREA"))
    if shape_area is not None:
        return shape_area * KM2_PER_DEG2
    return to_float(props.get("AREA_KM2")) or 0.0


def summarize_susceptibility_by_district(features: Iterable[Mapping]) -> dict[str, DistrictSusceptibility]:
    """Area of high and very-high susceptibility per district (attribute aggregation, no geometry)."""
    totals: dict[str, list[float]] = {}
    for feature in features:
        props = feature.get("properties", {})
        ubigeo = pad_ubigeo(props.get("CD_DIST"))
        grid = props.get("GRIDCODE")
        area = _polygon_area_km2(props)
        if not ubigeo or not isinstance(grid, (int, float)) or grid < 1:
            continue
        acc = totals.setdefault(ubigeo, [0.0, 0.0])
        acc[0] += area
        if grid >= HIGH_SUSCEPTIBILITY_MIN_CLASS:
            acc[1] += area
    return {u: DistrictSusceptibility(total_area_km2=a[0], high_area_km2=a[1]) for u, a in totals.items()}


# --- ANA critical points -----------------------------------------------------------


@dataclass(frozen=True)
class CriticalPoint:
    lon: float
    lat: float
    ubigeo: str
    district: str
    river: str
    period: int | None
    hazard_types: str
    hazard_level: str
    homes: int


def parse_critical_points(geojson: Mapping) -> list[CriticalPoint]:
    out: list[CriticalPoint] = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        props = feature.get("properties", {})
        lon, lat = geometry["coordinates"][:2]
        period = to_float(props.get("Periodo"))
        out.append(
            CriticalPoint(
                lon=float(lon),
                lat=float(lat),
                ubigeo=pad_ubigeo(props.get("Ubigeo")),
                district=clean(props.get("Distrito")),
                river=clean(props.get("Rio_QDA")),
                period=int(period) if period is not None else None,
                hazard_types=clean(props.get("TiposPeligro")),
                hazard_level=clean(props.get("Peligro")),
                homes=int(to_float(props.get("Viviendas")) or 0),
            )
        )
    return out


# --- CENEPRED El Nino 2027 scenario (comparison baseline only) ---------------------


@dataclass(frozen=True)
class CenepredDistrict:
    ubigeo: str
    name: str
    province: str
    department: str
    population_2017: int
    households_2017: int
    flood_risk_value: float | None
    flood_risk_level: str
    mm_risk_value: float | None
    mm_risk_level: str
    area_km2: float | None


def parse_cenepred_rows(rows: Iterable[Mapping[str, str]]) -> list[CenepredDistrict]:
    out: list[CenepredDistrict] = []
    for row in rows:
        ubigeo = pad_ubigeo(row.get("ubigeo"))
        if not ubigeo:
            continue
        out.append(
            CenepredDistrict(
                ubigeo=ubigeo,
                name=clean(row.get("dist")),
                province=clean(row.get("prov")),
                department=clean(row.get("dep")),
                population_2017=int(to_float(row.get("pob_2017")) or 0),
                households_2017=int(to_float(row.get("viv_2017")) or 0),
                flood_risk_value=to_float(row.get("flood_riesgo_val")),
                flood_risk_level=clean(row.get("flood_riesgo_nivel")),
                mm_risk_value=to_float(row.get("mm_riesgo_val")),
                mm_risk_level=clean(row.get("mm_riesgo_nivel")),
                area_km2=to_float(row.get("area_km2")),
            )
        )
    return out


def load_cenepred_csv(path: Path) -> list[CenepredDistrict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return parse_cenepred_rows(csv.DictReader(fh))

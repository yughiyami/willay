"""Deterministic safe-point proposal for one settlement (pure functions, stdlib only).

A "safe point" here is only a PROPOSAL computed from a DEM, never a surveyed or approved location. The
nearest grid cell (straight-line distance from the settlement) that satisfies all of these rules wins:

* at least `min_height_above_flow_m` above the elevation of the nearest flow-path (river) cell,
* at least `min_distance_from_flow_m` away from any flow path,
* at most `max_distance_m` from the settlement in a straight line,
* a slope strictly below `max_slope_deg`.

The grid is a small window cut out of the DEM by an adapter; it is passed in as plain tuples so the same
code runs on a 41x41 synthetic grid in tests and on a real clipped Copernicus DEM window in the offline build.
If the flow path elevation cannot be established, nothing is proposed (we never guess).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from willay.domain.geo import EARTH_RADIUS_M, haversine_m

METERS_PER_DEGREE = math.pi / 180.0 * EARTH_RADIUS_M
_BUCKET_M = 100.0


@dataclass(frozen=True)
class SafePointConfig:
    min_height_above_flow_m: float = 30.0
    min_distance_from_flow_m: float = 300.0
    max_distance_m: float = 2_000.0
    max_slope_deg: float = 20.0

    def __post_init__(self) -> None:
        if self.min_height_above_flow_m < 0 or self.min_distance_from_flow_m < 0:
            raise ValueError("min_height_above_flow_m and min_distance_from_flow_m must be >= 0")
        if self.max_distance_m <= 0:
            raise ValueError("max_distance_m must be > 0")
        if not 0 < self.max_slope_deg <= 90:
            raise ValueError("max_slope_deg must be within (0, 90]")


@dataclass(frozen=True)
class ElevationGrid:
    """A north-up window of a DEM. `values[row][col]` is metres or None for no data.

    `origin_lon`/`origin_lat` are the west and north edges of cell (0, 0); cell sizes are in degrees.
    """

    origin_lon: float
    origin_lat: float
    res_lon: float
    res_lat: float
    values: tuple[tuple[float | None, ...], ...]

    def __post_init__(self) -> None:
        if self.res_lon <= 0 or self.res_lat <= 0:
            raise ValueError("cell sizes must be > 0")
        if not self.values or not self.values[0]:
            raise ValueError("the grid needs at least one cell")

    @property
    def rows(self) -> int:
        return len(self.values)

    @property
    def cols(self) -> int:
        return len(self.values[0])

    def cell_lonlat(self, row: int, col: int) -> tuple[float, float]:
        return self.origin_lon + (col + 0.5) * self.res_lon, self.origin_lat - (row + 0.5) * self.res_lat

    def cell_of(self, lon: float, lat: float) -> tuple[int, int] | None:
        col = math.floor((lon - self.origin_lon) / self.res_lon)
        row = math.floor((self.origin_lat - lat) / self.res_lat)
        if 0 <= row < self.rows and 0 <= col < self.cols:
            return row, col
        return None

    def elevation(self, row: int, col: int) -> float | None:
        if 0 <= row < self.rows and 0 <= col < self.cols:
            return self.values[row][col]
        return None


@dataclass(frozen=True)
class SafePoint:
    lon: float
    lat: float
    elevation_m: float
    distance_m: float  # straight line from the settlement
    bearing_deg: float  # 0 = north, 90 = east, from the settlement to the point
    height_above_flow_m: float
    distance_to_flow_m: float
    slope_deg: float


def bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


class _FlowSamples:
    """Flow-path points (planar metres around the settlement) with their DEM elevation, bucketed for nearest search."""

    def __init__(self, samples: Sequence[tuple[float, float, float]]) -> None:
        self.samples = samples
        self.buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        for x, y, z in samples:
            self.buckets.setdefault((math.floor(x / _BUCKET_M), math.floor(y / _BUCKET_M)), []).append((x, y, z))
        keys = list(self.buckets)
        self.min_i = min(i for i, _ in keys) if keys else 0
        self.max_i = max(i for i, _ in keys) if keys else 0
        self.min_j = min(j for _, j in keys) if keys else 0
        self.max_j = max(j for _, j in keys) if keys else 0

    def nearest(self, x: float, y: float) -> tuple[float, float]:
        """(distance_m, elevation) of the nearest sample."""
        bi, bj = math.floor(x / _BUCKET_M), math.floor(y / _BUCKET_M)
        farthest = max(abs(bi - self.min_i), abs(bi - self.max_i), abs(bj - self.min_j), abs(bj - self.max_j)) + 1
        best_d2, best_z = math.inf, 0.0
        for ring in range(farthest + 1):
            for i in range(bi - ring, bi + ring + 1):
                edge = abs(i - bi) == ring
                for j in range(bj - ring, bj + ring + 1) if edge else sorted({bj - ring, bj + ring}):
                    for sx, sy, sz in self.buckets.get((i, j), ()):
                        d2 = (sx - x) ** 2 + (sy - y) ** 2
                        if d2 < best_d2 or (d2 == best_d2 and sz < best_z):
                            best_d2, best_z = d2, sz
            # anything in a farther ring is at least `ring * bucket` away
            if best_d2 <= (ring * _BUCKET_M) ** 2:
                break
        return math.sqrt(best_d2), best_z


def _flow_samples(
    grid: ElevationGrid, flow_lines: Sequence[Sequence[tuple[float, float]]], lon0: float, lat0: float, step_m: float
) -> _FlowSamples:
    kx = METERS_PER_DEGREE * math.cos(math.radians(lat0))
    ky = METERS_PER_DEGREE
    west, east = grid.origin_lon, grid.origin_lon + grid.cols * grid.res_lon
    north, south = grid.origin_lat, grid.origin_lat - grid.rows * grid.res_lat
    out: list[tuple[float, float, float]] = []
    for line in flow_lines:
        for (ax, ay), (bx, by) in zip(line, line[1:]):
            if max(ax, bx) < west or min(ax, bx) > east or max(ay, by) < south or min(ay, by) > north:
                continue
            length = math.hypot((bx - ax) * kx, (by - ay) * ky)
            n = max(1, math.ceil(length / step_m))
            for k in range(n + 1):
                t = k / n
                lon, lat = ax + (bx - ax) * t, ay + (by - ay) * t
                cell = grid.cell_of(lon, lat)
                if cell is None:
                    continue
                z = grid.elevation(*cell)
                if z is None:
                    continue
                out.append(((lon - lon0) * kx, (lat - lat0) * ky, z))
    return _FlowSamples(out)


def _slope_deg(grid: ElevationGrid, row: int, col: int) -> float | None:
    """Horn slope from the 3x3 neighbourhood, or None when any neighbour is missing."""
    window = []
    for dr in (-1, 0, 1):
        line = []
        for dc in (-1, 0, 1):
            z = grid.elevation(row + dr, col + dc)
            if z is None:
                return None
            line.append(z)
        window.append(line)
    (a, b, c), (d, _, f), (g, h, i) = window
    _, lat = grid.cell_lonlat(row, col)
    dx = grid.res_lon * METERS_PER_DEGREE * math.cos(math.radians(lat))
    dy = grid.res_lat * METERS_PER_DEGREE
    dz_dx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * dx)
    dz_dy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * dy)
    return math.degrees(math.atan(math.hypot(dz_dx, dz_dy)))


def select_safe_point(
    lon: float,
    lat: float,
    grid: ElevationGrid,
    flow_lines: Sequence[Sequence[tuple[float, float]]],
    config: SafePointConfig | None = None,
) -> SafePoint | None:
    """Nearest cell satisfying every rule, or None. `flow_lines` are polylines of (lon, lat)."""
    cfg = config or SafePointConfig()
    cell_m = min(grid.res_lon * METERS_PER_DEGREE * math.cos(math.radians(lat)), grid.res_lat * METERS_PER_DEGREE)
    flow = _flow_samples(grid, flow_lines, lon, lat, max(cell_m / 3.0, 1.0))
    if not flow.samples:
        return None
    lowest_flow = min(z for _, _, z in flow.samples)
    kx = METERS_PER_DEGREE * math.cos(math.radians(lat))
    ky = METERS_PER_DEGREE

    # Bounding box of the reachable cells, then sort by straight-line distance (row, col break ties).
    reach_lat = cfg.max_distance_m / ky
    reach_lon = cfg.max_distance_m / kx
    r0 = max(0, math.floor((grid.origin_lat - (lat + reach_lat)) / grid.res_lat))
    r1 = min(grid.rows - 1, math.floor((grid.origin_lat - (lat - reach_lat)) / grid.res_lat))
    c0 = max(0, math.floor((lon - reach_lon - grid.origin_lon) / grid.res_lon))
    c1 = min(grid.cols - 1, math.floor((lon + reach_lon - grid.origin_lon) / grid.res_lon))
    candidates: list[tuple[float, int, int]] = []
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            clon, clat = grid.cell_lonlat(row, col)
            distance = haversine_m(lon, lat, clon, clat)
            if distance <= cfg.max_distance_m:
                candidates.append((round(distance, 3), row, col))  # mm precision: float noise must not decide ties
    candidates.sort()

    for _, row, col in candidates:
        z = grid.elevation(row, col)
        if z is None or z - lowest_flow < cfg.min_height_above_flow_m:
            continue
        slope = _slope_deg(grid, row, col)
        if slope is None or slope >= cfg.max_slope_deg:
            continue
        clon, clat = grid.cell_lonlat(row, col)
        distance = haversine_m(lon, lat, clon, clat)
        flow_distance, flow_z = flow.nearest((clon - lon) * kx, (clat - lat) * ky)
        if flow_distance < cfg.min_distance_from_flow_m or z - flow_z < cfg.min_height_above_flow_m:
            continue
        return SafePoint(
            lon=clon,
            lat=clat,
            elevation_m=z,
            distance_m=distance,
            bearing_deg=bearing_deg(lon, lat, clon, clat),
            height_above_flow_m=z - flow_z,
            distance_to_flow_m=flow_distance,
            slope_deg=slope,
        )
    return None

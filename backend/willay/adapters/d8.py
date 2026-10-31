"""D8 steepest-descent flow tracing on an elevation grid (stdlib only).

The grid is reached through an accessor so the same code runs on a tiny list-of-lists in tests
and on a 10-million-cell numpy array in the offline build script. Pits and flats are breached
minimally: the trace searches outwards (priority-flood order) for the nearest cell lower than the
pit through the lowest saddle, and carves a channel to it. Only the cells around the trace are
ever read, so no whole-DEM sink filling is needed.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Callable, Sequence

from willay.domain.geo import haversine_m

ElevationFn = Callable[[int, int], "float | None"]
CellLonLatFn = Callable[[int, int], "tuple[float, float]"]

_SQRT2 = math.sqrt(2.0)
# (d_row, d_col, distance factor in cells). The order is the deterministic tie-break.
_NEIGHBOURS = (
    (-1, 0, 1.0),
    (0, 1, 1.0),
    (1, 0, 1.0),
    (0, -1, 1.0),
    (-1, 1, _SQRT2),
    (1, 1, _SQRT2),
    (1, -1, _SQRT2),
    (-1, -1, _SQRT2),
)


@dataclass(frozen=True)
class D8Config:
    max_length_m: float = 80_000.0
    sea_level_m: float = 0.0
    # Maximum number of cells examined while looking for the outlet of one pit.
    breach_search_limit: int = 300_000


@dataclass(frozen=True)
class TraceResult:
    cells: tuple[tuple[int, int], ...]
    cumulative_m: tuple[float, ...]
    # "sea" | "edge" | "max_length" | "unresolved_pit" | "loop"
    stop_reason: str
    breached_cells: int = 0


class _Tracer:
    def __init__(
        self, elevation: ElevationFn, rows: int, cols: int, cell_lonlat: CellLonLatFn, config: D8Config
    ) -> None:
        self.elevation, self.rows, self.cols = elevation, rows, cols
        self.cell_lonlat, self.cfg = cell_lonlat, config
        self.cells: list[tuple[int, int]] = []
        self.cum: list[float] = []
        self.visited: set[tuple[int, int]] = set()
        self.breached = 0

    def inside(self, r: int, c: int) -> bool:
        return 0 <= r < self.rows and 0 <= c < self.cols

    def on_boundary(self, r: int, c: int) -> bool:
        return r in (0, self.rows - 1) or c in (0, self.cols - 1)

    def push(self, cell: tuple[int, int]) -> bool:
        """Append a cell; return False when that would exceed the maximum length."""
        if self.cells:
            (r0, c0), (r1, c1) = self.cells[-1], cell
            lon0, lat0 = self.cell_lonlat(r0, c0)
            lon1, lat1 = self.cell_lonlat(r1, c1)
            distance = self.cum[-1] + haversine_m(lon0, lat0, lon1, lat1)
            if distance > self.cfg.max_length_m:
                return False
        else:
            distance = 0.0
        self.cells.append(cell)
        self.cum.append(distance)
        self.visited.add(cell)
        return True

    def steepest_lower(self, r: int, c: int, z: float) -> tuple[tuple[int, int] | None, bool]:
        """Best strictly-lower neighbour, and whether a nodata neighbour touches the cell."""
        best: tuple[int, int] | None = None
        best_slope = 0.0
        touches_nodata = False
        for dr, dc, factor in _NEIGHBOURS:
            nr, nc = r + dr, c + dc
            if not self.inside(nr, nc):
                continue
            zn = self.elevation(nr, nc)
            if zn is None:
                touches_nodata = True
                continue
            slope = (z - zn) / factor
            if zn < z and slope > best_slope:
                best, best_slope = (nr, nc), slope
        return best, touches_nodata

    def find_outlet(self, pit: tuple[int, int], pit_z: float) -> list[tuple[int, int]] | None:
        """Cells from the pit (excluded) to the nearest lower cell via the lowest saddle."""
        counter = 0
        heap: list[tuple[float, int, int, int]] = [(pit_z, counter, pit[0], pit[1])]
        parent: dict[tuple[int, int], tuple[int, int] | None] = {pit: None}
        examined = 0
        while heap:
            key, _, r, c = heapq.heappop(heap)
            examined += 1
            if examined > self.cfg.breach_search_limit:
                return None
            z = self.elevation(r, c)
            if (r, c) != pit and z is not None and z < pit_z:
                path: list[tuple[int, int]] = []
                node: tuple[int, int] | None = (r, c)
                while node is not None and node != pit:
                    path.append(node)
                    node = parent[node]
                path.reverse()
                return path
            for dr, dc, _ in _NEIGHBOURS:
                nr, nc = r + dr, c + dc
                if not self.inside(nr, nc) or (nr, nc) in parent:
                    continue
                zn = self.elevation(nr, nc)
                if zn is None:
                    continue
                parent[(nr, nc)] = (r, c)
                counter += 1
                heapq.heappush(heap, (max(key, zn), counter, nr, nc))
        return None

    def run(self, start: tuple[int, int]) -> TraceResult:
        z0 = self.elevation(*start)
        if z0 is None:
            raise ValueError(f"start cell {start} has no elevation")
        self.push(start)
        current, z = start, z0
        while True:
            if z <= self.cfg.sea_level_m:
                return self.result("sea")
            nxt, touches_nodata = self.steepest_lower(current[0], current[1], z)
            if nxt is not None:
                if nxt in self.visited:
                    return self.result("loop")
                if not self.push(nxt):
                    return self.result("max_length")
                current, z = nxt, self.elevation(*nxt)  # type: ignore[assignment]
            elif touches_nodata:
                return self.result("edge")
            else:
                outlet = self.find_outlet(current, z)
                if outlet is None:
                    return self.result("unresolved_pit")
                for cell in outlet:
                    if not self.push(cell):
                        return self.result("max_length")
                self.breached += len(outlet) - 1
                current = outlet[-1]
                z = self.elevation(*current)  # type: ignore[assignment]
            if self.on_boundary(*current):
                return self.result("sea" if z <= self.cfg.sea_level_m else "edge")

    def result(self, reason: str) -> TraceResult:
        return TraceResult(
            cells=tuple(self.cells),
            cumulative_m=tuple(self.cum),
            stop_reason=reason,
            breached_cells=self.breached,
        )


def trace_d8(
    elevation: ElevationFn,
    rows: int,
    cols: int,
    start: tuple[int, int],
    cell_lonlat: CellLonLatFn,
    config: D8Config | None = None,
) -> TraceResult:
    """Trace the D8 steepest-descent path from `start` until sea, the grid edge or the length cap."""
    return _Tracer(elevation, rows, cols, cell_lonlat, config or D8Config()).run(start)


def simplify_trace(
    coords: Sequence[tuple[float, float]], cumulative_m: Sequence[float], tolerance_m: float
) -> tuple[list[tuple[float, float]], list[float]]:
    """Douglas-Peucker simplification that keeps the cumulative distance of every kept vertex."""
    if len(coords) != len(cumulative_m):
        raise ValueError("coords and cumulative_m must have the same length")
    n = len(coords)
    if n <= 2:
        return list(coords), list(cumulative_m)
    mean_lat = math.radians(sum(lat for _, lat in coords) / n)
    kx, ky = 111_320.0 * math.cos(mean_lat), 110_574.0
    xy = [(lon * kx, lat * ky) for lon, lat in coords]

    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        worst, worst_i = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if seg2 == 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > worst:
                worst, worst_i = d, i
        if worst > tolerance_m:
            keep[worst_i] = True
            stack.append((a, worst_i))
            stack.append((worst_i, b))
    idx = [i for i in range(n) if keep[i]]
    return [coords[i] for i in idx], [cumulative_m[i] for i in idx]

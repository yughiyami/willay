"""Cut a small window out of a DEM array around a point (adapter for `willay.domain.safe_point`).

The DEM is any 2-D array supporting slicing and `.tolist()` (a numpy array in the offline build). Nodata
(values below `nodata_below`, the Copernicus GLO-30 clip uses -9999) becomes None.
"""

from __future__ import annotations

import math
from typing import Any

from willay.domain.safe_point import METERS_PER_DEGREE, ElevationGrid

NODATA_BELOW = -9000.0


def extract_window(
    dem: Any,
    origin_ul: tuple[float, float],
    res: tuple[float, float],
    lon: float,
    lat: float,
    radius_m: float,
    nodata_below: float = NODATA_BELOW,
) -> ElevationGrid | None:
    """Square window of half-size `radius_m` centred on (lon, lat); None when the point is outside the DEM."""
    rows, cols = dem.shape
    ox, oy = origin_ul
    res_x, res_y = res
    centre_col = math.floor((lon - ox) / res_x)
    centre_row = math.floor((oy - lat) / res_y)
    if not (0 <= centre_row < rows and 0 <= centre_col < cols):
        return None
    half_cols = math.ceil(radius_m / (res_x * METERS_PER_DEGREE * math.cos(math.radians(lat))))
    half_rows = math.ceil(radius_m / (res_y * METERS_PER_DEGREE))
    r0, r1 = max(0, centre_row - half_rows), min(rows, centre_row + half_rows + 1)
    c0, c1 = max(0, centre_col - half_cols), min(cols, centre_col + half_cols + 1)
    block = dem[r0:r1, c0:c1].tolist()
    values = tuple(tuple(None if v < nodata_below else float(v) for v in row) for row in block)
    return ElevationGrid(origin_lon=ox + c0 * res_x, origin_lat=oy - r0 * res_y, res_lon=res_x, res_lat=res_y, values=values)

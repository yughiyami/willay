"""Pure geodesy helpers (no GIS libraries)."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in meters between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def project_point_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[tuple[float, float], float]:
    """Project point P onto segment AB using a local equirectangular approximation.

    Inputs and outputs are (lon, lat) in degrees. Returns ((lon, lat), t) where
    t in [0, 1] is the fractional position of the projection along AB.
    Accurate for the short segments (km scale) used by river flow paths.
    """
    mean_lat = math.radians((ay + by + py) / 3.0)
    kx = math.cos(mean_lat)
    abx, aby = (bx - ax) * kx, by - ay
    apx, apy = (px - ax) * kx, py - ay
    denom = abx * abx + aby * aby
    if denom == 0.0:
        return (ax, ay), 0.0
    t = (apx * abx + apy * aby) / denom
    if t <= 0.0:
        return (ax, ay), 0.0
    if t >= 1.0:
        return (bx, by), 1.0
    return (ax + (bx - ax) * t, ay + (by - ay) * t), t

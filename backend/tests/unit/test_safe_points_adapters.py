"""Adapters around the safe-point selection: DEM window cut-out and the derived GeoJSON file."""

import json

import numpy as np
import pytest

from willay.adapters.dem_window import extract_window
from willay.adapters.safe_points_file import build_collection, load_proposed_safe_points
from willay.domain.safe_point import SafePoint, SafePointConfig, select_safe_point

SAFE = SafePoint(
    lon=-76.1, lat=-12.1, elevation_m=140.0, distance_m=900.0, bearing_deg=10.0,
    height_above_flow_m=44.0, distance_to_flow_m=350.0, slope_deg=7.5,
)

ORIGIN_UL = (-76.0, -12.0)
RES = (0.0003, 0.0003)


def test_extract_window_cuts_the_cells_around_a_point_and_maps_nodata_to_none():
    dem = np.arange(100 * 100, dtype="float32").reshape(100, 100)
    dem[50, 50] = -9999.0
    lon, lat = ORIGIN_UL[0] + 50.5 * RES[0], ORIGIN_UL[1] - 50.5 * RES[1]
    grid = extract_window(dem, ORIGIN_UL, RES, lon, lat, radius_m=100.0)
    assert grid is not None
    assert grid.res_lon == RES[0] and grid.res_lat == RES[1]
    # radius 100 m is ~3 cells: the window is square and centred on the point
    assert grid.rows == grid.cols and grid.rows >= 7
    centre = grid.cell_of(lon, lat)
    assert grid.elevation(*centre) is None  # the nodata cell
    assert grid.cell_lonlat(*centre) == pytest.approx((lon, lat))
    other = grid.cell_of(lon + RES[0], lat)
    assert grid.elevation(*other) == pytest.approx(float(dem[50, 51]))


def test_extract_window_clips_at_the_dem_edge_and_is_none_outside_it():
    dem = np.zeros((20, 20), dtype="float32")
    corner = extract_window(dem, ORIGIN_UL, RES, ORIGIN_UL[0] + 0.5 * RES[0], ORIGIN_UL[1] - 0.5 * RES[1], 200.0)
    assert corner is not None and corner.rows < 20 and corner.origin_lon == ORIGIN_UL[0]
    assert extract_window(dem, ORIGIN_UL, RES, -70.0, -5.0, 200.0) is None


def test_extracted_window_feeds_the_selection():
    rows = cols = 80
    dem = np.fromfunction(lambda r, c: 100.0 + 2.0 * np.abs(c - 40), (rows, cols), dtype="float32")
    river_lon = ORIGIN_UL[0] + 40.5 * RES[0]
    lon, lat = ORIGIN_UL[0] + 42.5 * RES[0], ORIGIN_UL[1] - 40.5 * RES[1]
    grid = extract_window(dem, ORIGIN_UL, RES, lon, lat, radius_m=700.0)
    top, bottom = ORIGIN_UL[1], ORIGIN_UL[1] - rows * RES[1]
    cfg = SafePointConfig(10.0, 100.0, 600.0, 20.0)
    point = select_safe_point(lon, lat, grid, [[(river_lon, top), (river_lon, bottom)]], cfg)
    assert point is not None and point.height_above_flow_m >= 10.0


def test_build_collection_records_found_and_not_found_settlements_and_the_rules():
    entries = [("ana-1", "Caserio Uno", -76.0, -12.0, SAFE), ("ana-2", "Caserio Dos", -76.2, -12.2, None)]
    collection = build_collection(entries, SafePointConfig())
    assert collection["type"] == "FeatureCollection"
    assert collection["properties"]["rules"]["min_height_above_flow_m"] == 30.0
    assert "validar con la municipalidad" in collection["properties"]["label"]
    found, missing = collection["features"]
    assert found["geometry"] == {"type": "Point", "coordinates": [-76.1, -12.1]}
    assert found["properties"]["found"] is True and found["properties"]["settlement_id"] == "ana-1"
    assert missing["geometry"] is None and missing["properties"]["found"] is False


def test_load_returns_only_found_points_and_roundtrips(tmp_path):
    path = tmp_path / "safe_points.geojson"
    entries = [("ana-1", "Caserio Uno", -76.0, -12.0, SAFE), ("ana-2", "Caserio Dos", -76.2, -12.2, None)]
    path.write_text(json.dumps(build_collection(entries, SafePointConfig())), encoding="utf-8")
    loaded = load_proposed_safe_points(path)
    assert set(loaded) == {"ana-1"}
    assert loaded["ana-1"].lon == pytest.approx(SAFE.lon)
    assert loaded["ana-1"].bearing_deg == pytest.approx(SAFE.bearing_deg)
    assert loaded["ana-1"].height_above_flow_m == pytest.approx(SAFE.height_above_flow_m)


def test_load_of_a_missing_or_broken_file_is_empty_not_an_error(tmp_path):
    assert load_proposed_safe_points(tmp_path / "nope.geojson") == {}
    broken = tmp_path / "broken.geojson"
    broken.write_text("{not json", encoding="utf-8")
    assert load_proposed_safe_points(broken) == {}

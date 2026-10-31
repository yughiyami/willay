"""Safe-point selection on tiny synthetic grids (pure domain logic, no real DEM)."""

import pytest

from willay.domain.geo import haversine_m
from willay.domain.safe_point import ElevationGrid, SafePointConfig, select_safe_point

RES = 0.0003  # ~33 m cells near the equator
LON0, LAT0 = -76.0, -12.0
M_PER_DEG = 111_195.0


def make_grid(elevation, rows=41, cols=41) -> ElevationGrid:
    values = tuple(tuple(elevation(r, c) for c in range(cols)) for r in range(rows))
    return ElevationGrid(origin_lon=LON0, origin_lat=LAT0, res_lon=RES, res_lat=RES, values=values)


def river_line(grid: ElevationGrid, col: int = 20):
    """A straight north-south river through the centre of column `col`."""
    lon, top = grid.cell_lonlat(0, col)
    _, bottom = grid.cell_lonlat(len(grid.values) - 1, col)
    return [[(lon, top), (lon, bottom)]]


# Small, test-sized rules: H=10 m, D=100 m, R=600 m, S=20 degrees.
CFG = SafePointConfig(
    min_height_above_flow_m=10.0, min_distance_from_flow_m=100.0, max_distance_m=600.0, max_slope_deg=20.0
)


def valley(r, c):
    """Gentle V valley: 2 m per cell away from the river column (~3.5 degrees)."""
    return 100.0 + 2.0 * abs(c - 20)


def test_defaults_match_the_agreed_rules():
    cfg = SafePointConfig()
    assert (cfg.min_height_above_flow_m, cfg.min_distance_from_flow_m, cfg.max_distance_m, cfg.max_slope_deg) == (
        30.0,
        300.0,
        2000.0,
        20.0,
    )


def test_config_rejects_invalid_rules():
    with pytest.raises(ValueError):
        SafePointConfig(max_distance_m=0)
    with pytest.raises(ValueError):
        SafePointConfig(min_height_above_flow_m=-1)
    with pytest.raises(ValueError):
        SafePointConfig(max_slope_deg=0)


def test_picks_the_nearest_cell_that_satisfies_every_rule():
    grid = make_grid(valley)
    origin = grid.cell_lonlat(20, 22)  # on the river bank
    point = select_safe_point(*origin, grid, river_line(grid), CFG)
    assert point is not None
    # height >= 10 m needs >= 5 cells from the river; the east side is 3 cells away, the west side 7.
    assert (point.lon, point.lat) == grid.cell_lonlat(20, 25)
    assert point.height_above_flow_m == pytest.approx(10.0)
    assert point.distance_to_flow_m >= 100.0
    assert point.distance_m == pytest.approx(haversine_m(*origin, point.lon, point.lat))
    assert point.bearing_deg == pytest.approx(90.0, abs=1.0)  # due east
    assert 0.0 < point.slope_deg < 20.0


def test_cells_not_high_enough_above_the_river_are_rejected_even_when_far_from_it():
    def shallow(r, c):  # rises 1 m per cell: 600 m away is only ~18 m above the river but 100 m away only 3 m
        return 100.0 + 1.0 * abs(c - 20)

    grid = make_grid(shallow)
    origin = grid.cell_lonlat(20, 22)
    short = SafePointConfig(
        min_height_above_flow_m=10.0, min_distance_from_flow_m=100.0, max_distance_m=150.0, max_slope_deg=20.0
    )
    assert select_safe_point(*origin, grid, river_line(grid), short) is None


def test_cells_closer_than_the_minimum_distance_to_a_flow_path_are_rejected():
    def terrace(r, c):  # a 20 m step right next to the river: the height rule holds everywhere beyond it
        return 100.0 + (20.0 if c != 20 else 0.0)

    grid = make_grid(terrace)
    origin = grid.cell_lonlat(20, 21)
    cfg = SafePointConfig(
        min_height_above_flow_m=10.0, min_distance_from_flow_m=100.0, max_distance_m=600.0, max_slope_deg=89.0
    )
    point = select_safe_point(*origin, grid, river_line(grid), cfg)
    assert point is not None
    assert point.distance_to_flow_m >= 100.0
    river_lon = grid.cell_lonlat(20, 20)[0]
    assert abs(point.lon - river_lon) * M_PER_DEG >= 100.0 - 1.0


def test_unreachable_points_beyond_the_straight_line_radius_are_rejected():
    grid = make_grid(valley)
    origin = grid.cell_lonlat(20, 22)
    tight = SafePointConfig(
        min_height_above_flow_m=10.0, min_distance_from_flow_m=100.0, max_distance_m=60.0, max_slope_deg=20.0
    )
    assert select_safe_point(*origin, grid, river_line(grid), tight) is None


def test_steep_cells_are_skipped_in_favour_of_the_next_gentle_one():
    def with_steep_band(r, c):
        # a 60 degree wall two cells wide, then a flat high terrace
        d = abs(c - 20)
        if d < 5:
            return 100.0 + 2.0 * d
        if d < 7:
            return 110.0 + (d - 4) * 40.0
        return 190.0

    grid = make_grid(with_steep_band)
    origin = grid.cell_lonlat(20, 22)
    point = select_safe_point(*origin, grid, river_line(grid), CFG)
    assert point is not None
    assert point.slope_deg < 20.0
    # the wall and the terrace cell whose 3x3 neighbourhood still touches it are skipped: first flat cell is 7 away
    assert (point.lon - grid.cell_lonlat(20, 20)[0]) / RES == pytest.approx(7.0)


def test_everywhere_too_steep_means_no_point():
    grid = make_grid(lambda r, c: 100.0 + 15.6 * abs(c - 20))  # ~25 degrees
    origin = grid.cell_lonlat(20, 22)
    assert select_safe_point(*origin, grid, river_line(grid), CFG) is None


def test_nodata_cells_are_skipped():
    def with_hole(r, c):
        if (r, c) == (20, 25):
            return None
        return valley(r, c)

    grid = make_grid(with_hole)
    origin = grid.cell_lonlat(20, 22)
    point = select_safe_point(*origin, grid, river_line(grid), CFG)
    assert point is not None and (point.lon, point.lat) != grid.cell_lonlat(20, 25)


def test_without_any_flow_path_nothing_can_be_verified_so_no_point_is_proposed():
    grid = make_grid(valley)
    assert select_safe_point(*grid.cell_lonlat(20, 22), grid, [], CFG) is None


def test_the_result_is_deterministic_and_ties_break_by_row_then_column():
    grid = make_grid(valley)
    origin = grid.cell_lonlat(20, 22)
    first = select_safe_point(*origin, grid, river_line(grid), CFG)
    assert first == select_safe_point(*origin, grid, river_line(grid), CFG)
    # Symmetric terrain, settlement exactly on the river: west and east are equally near; the lower column wins.
    on_river = select_safe_point(*grid.cell_lonlat(20, 20), grid, river_line(grid), CFG)
    assert on_river is not None and on_river.lon < grid.cell_lonlat(20, 20)[0]


def test_the_flow_path_elevation_is_taken_from_the_nearest_river_cell_not_the_global_minimum():
    # The river drops 40 m from north to south. A cell 10 m above the river HERE is not safe just because
    # it is 40 m above the far downstream end.
    def sloping_river(r, c):
        return 100.0 + (40 - r) * 1.0 + 2.0 * abs(c - 20)

    grid = make_grid(sloping_river)
    origin = grid.cell_lonlat(20, 22)
    point = select_safe_point(*origin, grid, river_line(grid), CFG)
    assert point is not None
    assert point.height_above_flow_m >= 10.0 - 1e-9


def test_points_outside_the_grid_do_not_crash():
    grid = make_grid(valley)
    assert select_safe_point(-70.0, -5.0, grid, river_line(grid), CFG) is None


def test_flow_paths_far_from_the_window_are_ignored_without_crashing():
    grid = make_grid(valley)
    far = [[(-60.0, -1.0), (-59.0, -2.0)]]
    assert select_safe_point(*grid.cell_lonlat(20, 22), grid, far, CFG) is None

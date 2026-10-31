import pytest

from willay.adapters.d8 import D8Config, simplify_trace, trace_d8

# One cell = 0.001 degrees of longitude/latitude (about 110 m); origin at lon -76.5, lat -11.5.
STEP = 0.001


def lonlat(r: int, c: int) -> tuple[float, float]:
    return -76.5 + c * STEP, -11.5 - r * STEP


def accessor(grid):
    rows, cols = len(grid), len(grid[0])

    def elevation(r: int, c: int):
        return grid[r][c] if 0 <= r < rows and 0 <= c < cols else None

    return elevation, rows, cols


def trace(grid, start, **cfg):
    elevation, rows, cols = accessor(grid)
    return trace_d8(elevation, rows, cols, start, lonlat, D8Config(**cfg))


def test_follows_the_steepest_descent_and_stops_at_the_grid_edge():
    grid = [[100 - c * 10 for c in range(6)] for _ in range(5)]  # falls to the east
    result = trace(grid, (2, 0))
    assert result.cells == tuple((2, c) for c in range(6))
    assert result.stop_reason == "edge"


def test_uses_slope_not_raw_drop_so_a_diagonal_must_beat_the_cardinal_by_sqrt2():
    # From (1,1)=40: east drops 10 over 1 cell; south-east drops 12 over sqrt(2) cells (8.5 per cell).
    grid = [
        [90, 90, 90, 90],
        [90, 40, 30, 90],
        [90, 90, 28, 90],
        [90, 90, 90, 90],
    ]
    assert trace(grid, (1, 1)).cells[1] == (1, 2)


def test_a_clearly_steeper_diagonal_is_chosen():
    grid = [
        [90, 90, 90, 90],
        [90, 40, 38, 90],
        [90, 90, 20, 90],
        [90, 90, 90, 90],
    ]
    assert trace(grid, (1, 1)).cells[1] == (2, 2)


def test_cumulative_distance_uses_haversine_between_cell_centres():
    grid = [[100 - c * 10 for c in range(4)] for _ in range(3)]
    result = trace(grid, (1, 0))
    assert result.cumulative_m[0] == 0.0
    # 0.001 degree of longitude at latitude -11.5 is about 108.9 m
    assert result.cumulative_m[1] == pytest.approx(108.9, rel=0.01)
    assert result.cumulative_m[-1] == pytest.approx(3 * 108.9, rel=0.01)


def test_stops_when_it_reaches_sea_level_and_includes_the_sea_cell():
    grid = [[30, 20, 10, 0, 0, 0]] * 3
    result = trace(grid, (1, 0))
    assert result.stop_reason == "sea"
    assert result.cells[-1] == (1, 3)


def test_stops_at_the_maximum_length_without_exceeding_it():
    grid = [[1000 - c for c in range(60)] for _ in range(3)]
    result = trace(grid, (1, 0), max_length_m=1_000.0)
    assert result.stop_reason == "max_length"
    assert result.cumulative_m[-1] <= 1_000.0
    assert len(result.cells) > 5


def test_breaches_a_pit_through_the_lowest_saddle_and_continues_downhill():
    grid = [
        [90, 90, 90, 90, 90, 90, 90],
        [90, 50, 60, 70, 40, 30, 20],
        [90, 90, 90, 90, 90, 90, 90],
    ]
    # Start at the pit (1,1)=50. The lowest saddle towards a cell below 50 is 70 at (1,3).
    result = trace(grid, (1, 1))
    assert result.cells[:5] == ((1, 1), (1, 2), (1, 3), (1, 4), (1, 5))
    assert result.cells[-1] == (1, 6)
    assert result.breached_cells == 2


def test_flat_ground_is_crossed_to_the_nearest_lower_cell():
    grid = [[10, 10, 10, 10, 5, 4, 3]] * 3
    result = trace(grid, (1, 0))
    assert (1, 4) in result.cells
    assert result.cells[-1] == (1, 6)


def test_closed_depression_with_a_small_search_limit_is_reported_unresolved():
    grid = [
        [90, 90, 90, 90, 90],
        [90, 10, 90, 90, 90],
        [90, 90, 90, 90, 90],
        [90, 90, 90, 0, 90],
        [90, 90, 90, 90, 90],
    ]
    result = trace(grid, (1, 1), breach_search_limit=3)
    assert result.stop_reason == "unresolved_pit"
    assert result.cells[-1] == (1, 1)


def test_nodata_neighbour_ends_the_trace_at_the_edge_of_valid_data():
    grid = [[50, 40, None, 10]] * 3
    result = trace(grid, (1, 0))
    assert result.stop_reason == "edge"
    assert result.cells[-1] == (1, 1)


def test_start_on_sea_returns_a_single_cell():
    result = trace([[0, 0], [0, 0]], (0, 0))
    assert result.cells == ((0, 0),)
    assert result.stop_reason == "sea"


def test_trace_is_deterministic():
    grid = [[(r * 7 + c * 3) % 11 + 50 - c for c in range(10)] for r in range(6)]
    assert trace(grid, (3, 0)) == trace(grid, (3, 0))


# --- simplification -----------------------------------------------------------


def test_simplify_keeps_endpoints_and_cumulative_distances():
    coords = [(-76.5 + i * 0.001, -11.5) for i in range(11)]
    cum = [i * 108.9 for i in range(11)]
    out_coords, out_cum = simplify_trace(coords, cum, tolerance_m=5.0)
    assert out_coords == [coords[0], coords[-1]]
    assert out_cum == [cum[0], cum[-1]]


def test_simplify_keeps_a_corner_larger_than_the_tolerance():
    coords = [(-76.5, -11.5), (-76.499, -11.5), (-76.498, -11.5), (-76.498, -11.501), (-76.498, -11.502)]
    cum = [0.0, 109.0, 218.0, 329.0, 440.0]
    out_coords, out_cum = simplify_trace(coords, cum, tolerance_m=10.0)
    assert (-76.498, -11.5) in out_coords
    assert len(out_coords) == len(out_cum) == 3


def test_simplify_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        simplify_trace([(0.0, 0.0), (1.0, 1.0)], [0.0], tolerance_m=1.0)

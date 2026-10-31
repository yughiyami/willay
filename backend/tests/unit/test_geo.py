import math

import pytest

from willay.domain.geo import haversine_m, project_point_on_segment


def test_haversine_zero_distance():
    assert haversine_m(-76.6, -11.8, -76.6, -11.8) == 0.0


def test_haversine_one_degree_latitude_is_about_111_km():
    d = haversine_m(-76.0, -11.0, -76.0, -12.0)
    assert d == pytest.approx(111_195, rel=1e-3)


def test_haversine_is_symmetric():
    a = haversine_m(-76.1, -11.9, -76.4, -11.7)
    b = haversine_m(-76.4, -11.7, -76.1, -11.9)
    assert a == pytest.approx(b)


def test_project_point_on_segment_midpoint():
    # Segment along a parallel; point slightly north of its middle.
    (px, py), t = project_point_on_segment(-76.5, -11.99, -76.6, -12.0, -76.4, -12.0)
    assert t == pytest.approx(0.5, abs=1e-6)
    assert px == pytest.approx(-76.5, abs=1e-6)
    assert py == pytest.approx(-12.0, abs=1e-6)


def test_project_point_clamps_to_segment_start():
    (px, py), t = project_point_on_segment(-77.0, -12.0, -76.6, -12.0, -76.4, -12.0)
    assert t == 0.0
    assert (px, py) == (-76.6, -12.0)


def test_project_point_clamps_to_segment_end():
    _, t = project_point_on_segment(-75.0, -12.0, -76.6, -12.0, -76.4, -12.0)
    assert t == 1.0


def test_project_point_degenerate_segment():
    (px, py), t = project_point_on_segment(-76.0, -12.0, -76.5, -12.0, -76.5, -12.0)
    assert (px, py) == (-76.5, -12.0)
    assert t == 0.0
    assert not math.isnan(t)

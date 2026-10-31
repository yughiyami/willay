import math

import pytest

from willay.domain.entities import FlowPath, PathPoint, Settlement, WaterIntake
from willay.domain.exposure import ExposureConfig, downstream_exposure
from willay.domain.geo import haversine_m

LON = -76.5


def meridian_path(start_lat=-11.8, end_lat=-12.4, step=0.1) -> FlowPath:
    """Straight north-to-south path along a meridian with haversine cumulative distance."""
    n = round(abs(end_lat - start_lat) / step)
    lats = [start_lat - i * step for i in range(n + 1)]
    points = []
    cum = 0.0
    for i, lat in enumerate(lats):
        if i:
            cum += haversine_m(LON, lats[i - 1], LON, lat)
        points.append(PathPoint(LON, lat, cum))
    return FlowPath(points=tuple(points))


def settlement_at(along_m: float, offset_deg_lon=0.0, **kwargs) -> Settlement:
    """Place a settlement `along_m` meters south of the path start (approx, meridian)."""
    lat = -11.8 - along_m / 111_195.0
    base = dict(id=f"s{int(along_m)}", name="Fake", lon=LON + offset_deg_lon, lat=lat, population=500, households=120)
    base.update(kwargs)
    return Settlement(**base)


CFG = ExposureConfig()


def test_no_settlements_means_zero_exposure():
    result = downstream_exposure(meridian_path(), [], CFG)
    assert result.value == 0.0
    assert result.contributions == ()


def test_settlement_on_path_is_snapped_with_distance_decay_weight():
    result = downstream_exposure(meridian_path(), [settlement_at(10_000)], CFG)
    (c,) = result.contributions
    assert c.along_path_m == pytest.approx(10_000, rel=0.01)
    assert c.snap_distance_m < 50
    assert c.weight == pytest.approx(math.exp(-1.0), rel=0.02)
    assert 0.0 < result.value <= 1.0


def test_closer_settlement_contributes_more_than_farther_one():
    near = downstream_exposure(meridian_path(), [settlement_at(2_000)], CFG)
    far = downstream_exposure(meridian_path(), [settlement_at(30_000)], CFG)
    assert near.value > far.value


def test_settlement_beyond_snap_tolerance_is_ignored():
    # 0.01 deg lon at -11.9 lat is about 1.09 km off the path.
    off = settlement_at(5_000, offset_deg_lon=0.01)
    result = downstream_exposure(meridian_path(), [off], CFG)
    assert result.contributions == ()
    assert result.value == 0.0


def test_snap_tolerance_is_configurable():
    off = settlement_at(5_000, offset_deg_lon=0.01)
    result = downstream_exposure(meridian_path(), [off], ExposureConfig(snap_tolerance_m=2_000))
    assert len(result.contributions) == 1


def test_settlement_beyond_cutoff_is_ignored():
    result = downstream_exposure(meridian_path(), [settlement_at(55_000)], CFG)
    assert result.contributions == ()


def test_cutoff_is_configurable():
    result = downstream_exposure(meridian_path(), [settlement_at(55_000)], ExposureConfig(cutoff_m=60_000))
    assert len(result.contributions) == 1


def test_settlement_upstream_of_the_site_is_not_exposed():
    upstream = Settlement(id="up", name="Up", lon=LON, lat=-11.7, population=5000, households=1000)
    result = downstream_exposure(meridian_path(), [upstream], CFG)
    assert result.contributions == ()


def test_water_intake_increases_exposure():
    plain = downstream_exposure(meridian_path(), [settlement_at(5_000)], CFG)
    with_intake = downstream_exposure(meridian_path(), [settlement_at(5_000, has_water_intake=True)], CFG)
    assert with_intake.value > plain.value


def test_more_population_increases_exposure():
    small = downstream_exposure(meridian_path(), [settlement_at(5_000, population=50, households=12)], CFG)
    large = downstream_exposure(meridian_path(), [settlement_at(5_000, population=5_000, households=1_200)], CFG)
    assert large.value > small.value


def test_households_used_when_population_is_unknown():
    result = downstream_exposure(
        meridian_path(), [settlement_at(5_000, population=0, households=200)], CFG
    )
    assert result.value > 0.0


def test_exposure_is_bounded_and_saturates_with_many_settlements():
    many = [
        settlement_at(1_000 * i, id=f"s{i}", population=20_000, households=5_000, has_water_intake=True)
        for i in range(1, 40)
    ]
    result = downstream_exposure(meridian_path(), many, CFG)
    assert 0.9 < result.value <= 1.0


def test_contributions_are_sorted_by_along_path_distance():
    result = downstream_exposure(
        meridian_path(), [settlement_at(20_000), settlement_at(3_000), settlement_at(9_000)], CFG
    )
    along = [c.along_path_m for c in result.contributions]
    assert along == sorted(along)
    assert result.nearest is not None
    assert result.nearest.settlement_id == "s3000"


def test_decay_length_is_configurable():
    s = [settlement_at(10_000)]
    short = downstream_exposure(meridian_path(), s, ExposureConfig(decay_length_m=2_000))
    long = downstream_exposure(meridian_path(), s, ExposureConfig(decay_length_m=20_000))
    assert long.value > short.value


def test_config_validation():
    with pytest.raises(ValueError):
        ExposureConfig(decay_length_m=0)
    with pytest.raises(ValueError):
        ExposureConfig(snap_tolerance_m=-1)
    with pytest.raises(ValueError):
        ExposureConfig(cutoff_m=0)


# --- water intakes ----------------------------------------------------------


def intake_at(along_m: float, offset_deg_lon=0.0, **kwargs) -> WaterIntake:
    lat = -11.8 - along_m / 111_195.0
    base = dict(id=f"i{int(along_m)}", name="Fake intake", lon=LON + offset_deg_lon, lat=lat)
    base.update(kwargs)
    return WaterIntake(**base)


def test_intake_on_path_is_reported_with_distance_and_raises_exposure():
    path = meridian_path()
    without = downstream_exposure(path, [], CFG)
    with_intake = downstream_exposure(path, [], CFG, intakes=[intake_at(8_000)])
    (reached,) = with_intake.intakes
    assert reached.intake_id == "i8000"
    assert reached.along_path_m == pytest.approx(8_000, rel=0.01)
    assert reached.approximate is True
    assert with_intake.value > without.value == 0.0
    assert with_intake.contributions == ()


def test_intake_off_the_path_or_beyond_cutoff_is_ignored():
    path = meridian_path(end_lat=-12.6)
    far_beside = intake_at(5_000, offset_deg_lon=0.05)  # about 5.4 km sideways
    too_far = intake_at(60_000)
    result = downstream_exposure(path, [], CFG, intakes=[far_beside, too_far])
    assert result.intakes == ()
    assert result.value == 0.0


def test_intake_impact_decays_with_distance():
    path = meridian_path()
    near = downstream_exposure(path, [], CFG, intakes=[intake_at(2_000)])
    far = downstream_exposure(path, [], CFG, intakes=[intake_at(20_000)])
    assert near.value > far.value


def test_intake_exposure_keeps_the_noisy_or_bound():
    path = meridian_path()
    big = settlement_at(1_000, population=5_000_000, households=1_000_000)
    result = downstream_exposure(path, [big], CFG, intakes=[intake_at(1_500), intake_at(2_000)])
    assert 0.0 < result.value <= 1.0


def test_intakes_are_sorted_along_the_path_and_serialisable():
    path = meridian_path()
    result = downstream_exposure(path, [], CFG, intakes=[intake_at(9_000), intake_at(3_000)])
    assert [i.intake_id for i in result.intakes] == ["i3000", "i9000"]
    data = result.intakes[0].to_dict()
    assert data["distance_km"] == pytest.approx(3.0, rel=0.02)
    assert data["approximate"] is True

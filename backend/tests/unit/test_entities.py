from datetime import date

import pytest

from willay.domain.entities import (
    AlertTier,
    EnfenMagnitude,
    Emergency,
    FlowPath,
    PathPoint,
    RainfallAnomaly,
    RiskLevel,
    SenamhiLevel,
    Settlement,
    SiteStatus,
    TailingsSite,
    WaterIntake,
)


def test_enfen_magnitude_ordering_follows_scale():
    order = [
        EnfenMagnitude.NEUTRAL,
        EnfenMagnitude.WEAK,
        EnfenMagnitude.MODERATE,
        EnfenMagnitude.STRONG,
        EnfenMagnitude.EXTRAORDINARY,
    ]
    assert order == sorted(order)
    assert EnfenMagnitude.STRONG >= EnfenMagnitude.STRONG
    assert EnfenMagnitude.EXTRAORDINARY > EnfenMagnitude.STRONG
    assert EnfenMagnitude.WEAK < EnfenMagnitude.MODERATE


def test_senamhi_level_ordering():
    assert SenamhiLevel.RED > SenamhiLevel.ORANGE > SenamhiLevel.YELLOW > SenamhiLevel.GREEN


def test_alert_tier_values_are_stable_strings():
    assert AlertTier.PENDING_CONFIRMATION.value == "PENDING_CONFIRMATION"
    assert AlertTier.EVACUATION.value == "EVACUATION"


def test_flow_path_requires_two_points():
    with pytest.raises(ValueError):
        FlowPath(points=(PathPoint(-76.0, -11.0, 0.0),))


def test_flow_path_cumulative_distance_must_not_decrease():
    with pytest.raises(ValueError):
        FlowPath(points=(PathPoint(-76.0, -11.0, 100.0), PathPoint(-76.1, -11.0, 50.0)))


def test_flow_path_total_length():
    path = FlowPath(points=(PathPoint(-76.0, -11.0, 0.0), PathPoint(-76.1, -11.0, 1000.0)))
    assert path.total_length_m == 1000.0


def test_settlement_rejects_negative_population():
    with pytest.raises(ValueError):
        Settlement(id="s", name="x", lon=-76.0, lat=-11.0, population=-1, households=0)


def test_site_rejects_invalid_coordinates():
    with pytest.raises(ValueError):
        TailingsSite(
            id="t",
            name="x",
            lon=-200.0,
            lat=-11.0,
            volume_m3=1000.0,
            status=SiteStatus.ACTIVE,
            declared_risk=RiskLevel.LOW,
            distance_to_river_m=100.0,
            susceptibility=0.5,
        )


def test_site_rejects_susceptibility_out_of_range():
    with pytest.raises(ValueError):
        TailingsSite(
            id="t",
            name="x",
            lon=-76.0,
            lat=-11.0,
            volume_m3=1000.0,
            status=SiteStatus.ACTIVE,
            declared_risk=RiskLevel.LOW,
            distance_to_river_m=100.0,
            susceptibility=1.5,
        )


def test_rainfall_anomaly_members():
    assert {a.value for a in RainfallAnomaly} == {"below_normal", "normal", "above_normal"}


def test_site_defaults_have_no_data_quality_flags_and_are_in_basin():
    site = TailingsSite(
        id="t",
        name="x",
        lon=-76.0,
        lat=-11.0,
        volume_m3=1000.0,
        status=SiteStatus.UNKNOWN,
        declared_risk=RiskLevel.UNKNOWN,
        distance_to_river_m=100.0,
        susceptibility=0.5,
    )
    assert site.data_quality == ()
    assert site.in_basin is True
    assert site.area_m2 is None


def test_water_intake_is_flagged_approximate_by_default():
    intake = WaterIntake(id="i", name="Bocatoma", lon=-76.9, lat=-12.0)
    assert intake.approximate is True


def test_emergency_requires_a_valid_ubigeo():
    with pytest.raises(ValueError):
        Emergency(ubigeo="15", district="X", date=date(2017, 3, 15), hazard="HUAYCO")
    ok = Emergency(ubigeo="150118", district="X", date=date(2017, 3, 15), hazard="HUAYCO")
    assert ok.ubigeo == "150118"

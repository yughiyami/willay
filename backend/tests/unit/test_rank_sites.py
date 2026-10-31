import pytest

from willay.application.rank_sites import rank_sites, site_static_risk
from willay.domain.entities import (
    EnfenMagnitude,
    FlowPath,
    HazardContext,
    PathPoint,
    RainfallAnomaly,
    RiskLevel,
    Settlement,
    SiteStatus,
    TailingsSite,
    WaterIntake,
)
from willay.domain.exposure import ExposureConfig
from willay.domain.geo import haversine_m
from willay.domain.risk import RiskConfig


def path_from(lon: float, lat0: float, length_deg: float = 0.5) -> FlowPath:
    lat1 = lat0 - length_deg
    return FlowPath(points=(PathPoint(lon, lat0, 0.0), PathPoint(lon, lat1, haversine_m(lon, lat0, lon, lat1))))


def site(site_id: str, lon: float, **overrides) -> TailingsSite:
    base = dict(
        id=site_id,
        name=site_id,
        lon=lon,
        lat=-11.8,
        volume_m3=100_000,
        status=SiteStatus.ACTIVE,
        declared_risk=RiskLevel.MEDIUM,
        distance_to_river_m=300,
        susceptibility=0.5,
        flow_path=path_from(lon, -11.8),
    )
    base.update(overrides)
    return TailingsSite(**base)


class Repo:
    def __init__(self, sites, settlements):
        self._sites, self._settlements = list(sites), list(settlements)

    def list_sites(self):
        return self._sites

    def get_site(self, site_id):
        return next((s for s in self._sites if s.id == site_id), None)

    def list_settlements(self):
        return self._settlements


HOT = HazardContext(
    enfen_magnitude=EnfenMagnitude.STRONG, seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL
)


def test_sites_are_ranked_by_descending_risk():
    safe = site("safe", -76.5, status=SiteStatus.ACTIVE, declared_risk=RiskLevel.LOW, volume_m3=2_000, susceptibility=0.1)
    bad = site("bad", -76.6, status=SiteStatus.ABANDONED, declared_risk=RiskLevel.VERY_HIGH, volume_m3=3e6, susceptibility=0.9)
    settlements = [
        Settlement("a", "A", -76.5, -11.9, 3000, 700),
        Settlement("b", "B", -76.6, -11.9, 3000, 700),
    ]
    ranked = rank_sites(Repo([safe, bad], settlements), HOT)
    assert [r.site.id for r in ranked] == ["bad", "safe"]
    assert [r.rank for r in ranked] == [1, 2]
    assert ranked[0].score.value > ranked[1].score.value


def test_ranked_site_exposes_factor_breakdown_and_exposure_details():
    s = site("x", -76.5)
    ranked = rank_sites(Repo([s], [Settlement("a", "A", -76.5, -11.9, 3000, 700)]), HOT)
    (r,) = ranked
    assert set(r.score.factors) == {"trigger", "susceptibility", "tailings_vulnerability", "downstream_exposure"}
    assert r.exposure is not None and r.exposure.contributions
    assert r.static_risk > 0


def test_site_without_flow_path_gets_epsilon_exposure_and_is_flagged():
    s = site("nopath", -76.5, flow_path=None)
    (r,) = rank_sites(Repo([s], []), HOT, risk_config=RiskConfig(epsilon=0.1))
    assert r.exposure is None
    assert "downstream_exposure" in r.score.missing_factors
    assert r.score.factors["downstream_exposure"].value == 0.1


def test_site_with_path_but_no_settlements_has_zero_raw_exposure_floored_by_epsilon():
    s = site("empty", -76.5)
    (r,) = rank_sites(Repo([s], []), HOT, risk_config=RiskConfig(epsilon=0.1))
    assert r.score.factors["downstream_exposure"].raw == 0.0
    assert r.score.factors["downstream_exposure"].floored is True
    assert r.score.value > 0


def test_site_static_risk_matches_ranked_static_risk():
    s = site("x", -76.5)
    settlements = [Settlement("a", "A", -76.5, -11.9, 3000, 700)]
    (r,) = rank_sites(Repo([s], settlements), HOT)
    assert site_static_risk(s, settlements) == pytest.approx(r.static_risk)


def test_exposure_config_is_applied():
    s = site("x", -76.5)
    far = Settlement("a", "A", -76.5, -12.2, 3000, 700)  # about 44 km downstream
    default = rank_sites(Repo([s], [far]), HOT)[0]
    tight = rank_sites(Repo([s], [far]), HOT, exposure_config=ExposureConfig(cutoff_m=10_000))[0]
    assert default.exposure.value > 0
    assert tight.exposure.value == 0


def test_ranked_site_to_dict_is_json_friendly():
    s = site("x", -76.5, synthetic=True)
    (r,) = rank_sites(Repo([s], [Settlement("a", "A", -76.5, -11.9, 3000, 700)]), HOT)
    data = r.to_dict()
    assert data["id"] == "x"
    assert data["rank"] == 1
    assert data["synthetic"] is True
    assert "factors" in data["risk"]
    assert data["exposure"]["settlements"][0]["settlement_id"] == "a"


# --- real-data additions: intakes, data quality, basin filter ---------------


def test_reached_intakes_raise_exposure_and_are_listed():
    s = site("x", -76.5)
    intake = WaterIntake(id="i1", name="Bocatoma X", lon=-76.5, lat=-11.85)
    with_intake = rank_sites(Repo([s], []), HOT, intakes=[intake])[0]
    without = rank_sites(Repo([s], []), HOT)[0]
    assert with_intake.exposure.value > without.exposure.value
    assert [i.intake_id for i in with_intake.exposure.intakes] == ["i1"]
    data = with_intake.to_dict()
    assert data["water_intakes"][0]["approximate"] is True
    assert data["water_intakes"][0]["distance_km"] > 0


def test_to_dict_reports_data_quality_flags_and_settlement_distance():
    s = site("x", -76.5, data_quality=("risk_level_unknown",), in_basin=False, area_m2=1234.0)
    (r,) = rank_sites(Repo([s], [Settlement("a", "A", -76.5, -11.9, 3000, 700)]), HOT)
    data = r.to_dict()
    assert data["data_quality"] == ["risk_level_unknown"]
    assert data["in_basin"] is False
    assert data["area_m2"] == 1234.0
    reached = data["exposure"]["settlements"][0]
    assert reached["settlement_name"] == "A"
    assert reached["population"] == 3000
    assert reached["distance_km"] == pytest.approx(reached["along_path_km"])


def test_basin_only_filter_drops_sites_outside_the_basin():
    inside = site("in", -76.5)
    outside = site("out", -76.6, in_basin=False)
    ranked = rank_sites(Repo([inside, outside], []), HOT, basin_only=True)
    assert [r.site.id for r in ranked] == ["in"]
    assert len(rank_sites(Repo([inside, outside], []), HOT)) == 2

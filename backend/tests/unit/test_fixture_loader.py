import json

import pytest

from willay.adapters.fixture_loader import load_backtest_fixture, load_rimac_fixture
from willay.domain.entities import EnfenMagnitude, RainfallAnomaly, SenamhiLevel
from willay.domain.exposure import downstream_exposure


def test_rimac_fixture_is_flagged_synthetic_everywhere():
    fixture = load_rimac_fixture()
    assert fixture.synthetic is True
    assert "SYNTHETIC" in fixture.disclaimer
    assert 3 <= len(fixture.sites) <= 5
    assert all(site.synthetic for site in fixture.sites)
    assert all("SYNTHETIC" in site.name for site in fixture.sites)
    assert all("SYNTHETIC" in s.name for s in fixture.settlements)


def test_flow_paths_start_at_the_site_and_have_increasing_distance():
    fixture = load_rimac_fixture()
    for site in fixture.sites:
        assert site.flow_path is not None
        first = site.flow_path.points[0]
        assert (first.lon, first.lat) == (site.lon, site.lat)
        assert first.cumulative_distance_m == 0.0
        cums = [p.cumulative_distance_m for p in site.flow_path.points]
        assert cums == sorted(cums) and cums[-1] > 0


def test_hazard_scenario_is_loaded():
    scenario = load_rimac_fixture().hazard_scenario
    assert scenario.enfen_magnitude is EnfenMagnitude.STRONG
    assert scenario.seasonal_rainfall_anomaly is RainfallAnomaly.ABOVE_NORMAL
    assert scenario.senamhi_level is SenamhiLevel.YELLOW


def test_every_site_has_some_exposure_except_by_design_the_off_river_hamlet():
    fixture = load_rimac_fixture()
    for site in fixture.sites:
        result = downstream_exposure(site.flow_path, fixture.settlements)
        assert result.value > 0, site.id
        assert "set-006" not in {c.settlement_id for c in result.contributions}


def test_backtest_fixture_loads_and_is_synthetic():
    dataset = load_backtest_fixture()
    assert dataset.synthetic is True
    assert len(dataset.districts) >= 10
    assert any(d.emergency for d in dataset.districts)
    assert any(not d.emergency for d in dataset.districts)
    assert dataset.evacuation_events


def test_loader_rejects_fixture_not_flagged_synthetic(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"synthetic": False, "sites": [], "settlements": [], "rivers": {}}))
    with pytest.raises(ValueError, match="synthetic"):
        load_rimac_fixture(path)

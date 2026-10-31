"""API wired to a (tiny, synthetic) willay-data folder instead of the bundled fixtures."""

import pytest
from fastapi.testclient import TestClient

from tests.data_dir_factory import build_data_dir
from willay.adapters.data_dir import DataDir, DataDirError
from willay.api.app import create_app
from willay.api.dependencies import default_dependencies, real_dependencies


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    root = build_data_dir(tmp_path_factory.mktemp("willay-data"))
    return TestClient(create_app(real_dependencies(DataDir(root))))


def test_health_reports_real_data_mode(client):
    body = client.get("/health").json()
    assert body["synthetic_data"] is False
    assert body["data_mode"] == "real"


# --- /sites -------------------------------------------------------------------


def test_sites_default_to_the_basin_and_are_ranked(client):
    body = client.get("/sites").json()
    assert body["synthetic"] is False
    ids = [s["id"] for s in body["sites"]]
    assert set(ids) == {"ingemmet-1", "ingemmet-2", "ingemmet-4"}  # gamma is outside the basin
    values = [s["risk"]["value"] for s in body["sites"]]
    assert values == sorted(values, reverse=True)
    assert all(s["synthetic"] is False for s in body["sites"])


def test_sites_can_include_the_whole_bbox(client):
    ids = {s["id"] for s in client.get("/sites?basin_only=false").json()["sites"]}
    assert "ingemmet-3" in ids


def test_sites_expose_factor_breakdown_and_data_quality_flags(client):
    sites = {s["id"]: s for s in client.get("/sites").json()["sites"]}
    beta = sites["ingemmet-2"]
    assert {"status_unknown", "risk_level_unknown"} <= set(beta["data_quality"])
    assert set(beta["risk"]["factors"]) == {"trigger", "susceptibility", "tailings_vulnerability", "downstream_exposure"}
    assert sites["ingemmet-1"]["data_quality"] == []


def test_sites_detail_flag_adds_reached_settlements_and_intakes(client):
    plain = {s["id"]: s for s in client.get("/sites").json()["sites"]}
    assert plain["ingemmet-1"]["exposure"]["settlements"] == []
    detailed = {s["id"]: s for s in client.get("/sites?detail=true").json()["sites"]}
    assert detailed["ingemmet-1"]["exposure"]["settlements"]


def test_site_detail_lists_reached_settlements_intakes_and_the_flow_path(client):
    body = client.get("/sites/ingemmet-1").json()
    reached = {s["settlement_name"]: s for s in body["exposure"]["settlements"]}
    assert set(reached) == {"VILLA UNO", "VILLA DOS"}
    assert reached["VILLA UNO"]["population"] == 800
    assert reached["VILLA UNO"]["distance_km"] == pytest.approx(11.1, abs=0.3)
    (intake,) = body["water_intakes"]
    assert intake["approximate"] is True and intake["name"] == "Planta Test"
    assert body["flow_path"]["geometry"]["type"] == "LineString"
    assert body["flow_path"]["properties"]["cumulative_distance_m"][-1] == pytest.approx(22239.0)
    assert body["static_inputs"]["susceptibility_label"] == "Alta"
    assert body["static_inputs"]["nearest_river"] == "Quebrada Test"
    assert "alert" in body


def test_site_without_a_flow_path_has_a_null_path_and_flags(client):
    body = client.get("/sites/ingemmet-4").json()
    assert body["flow_path"] is None
    assert "no_flow_path" in body["data_quality"]


def test_unknown_site_is_404(client):
    assert client.get("/sites/nope").status_code == 404


# --- layers ---------------------------------------------------------------------


def test_flowpaths_are_served_as_a_feature_collection(client):
    body = client.get("/flowpaths").json()
    assert body["type"] == "FeatureCollection"
    assert {f["properties"]["site_id"] for f in body["features"]} == {"ingemmet-1", "ingemmet-2"}


def test_pasivos_are_a_flagged_secondary_layer(client):
    body = client.get("/layers/pasivos").json()
    assert body["secondary"] is True and body["inventory_year"] == 2011
    assert len(body["items"]) == 2
    assert all("minem_2011_inventory" in i["data_quality"] for i in body["items"])


def test_intakes_layer_is_flagged_approximate(client):
    body = client.get("/layers/intakes").json()
    assert "SEDAPAL" in body["note"]
    assert body["items"][0]["approximate"] is True


# --- /districts -------------------------------------------------------------------


def test_districts_are_a_feature_collection_with_score_baseline_and_emergencies(client):
    body = client.get("/districts").json()
    assert body["type"] == "FeatureCollection"
    assert "weights" in body["scoring"]
    props = {f["properties"]["ubigeo"]: f["properties"] for f in body["features"]}
    assert set(props) == {"150722", "150705", "150401"}
    san_mateo = props["150722"]
    assert 0.0 <= san_mateo["static_score"] <= 1.0
    assert set(san_mateo["components"]) == {"mm_high_share", "mean_slope_deg", "exposed_population", "tailings_upstream"}
    assert san_mateo["cenepred"]["mm_risk_level"] == "Alto"
    assert san_mateo["emergencies"]["2017"] == {"count": 2, "first_date": "2017-03-15"}
    assert props["150705"]["emergencies"]["2017"]["count"] == 0  # a fire is not a target hazard
    assert props["150705"]["emergencies"]["2023"] == {"count": 1, "first_date": "2023-03-14"}
    assert body["features"][0]["geometry"]["type"] == "Polygon"


def test_static_score_orders_districts_by_our_inputs_not_by_cenepred(client):
    props = {f["properties"]["ubigeo"]: f["properties"] for f in client.get("/districts").json()["features"]}
    assert props["150722"]["static_score"] > props["150401"]["static_score"]


# --- /backtest ---------------------------------------------------------------------


def test_backtest_get_serves_the_stored_event(client):
    body = client.get("/backtest?event=2017").json()
    assert body["event"] == "2017"
    assert body["models"]["willay_static"]["auc"] == 0.8
    assert body["data_mode"] == "real"


def test_backtest_without_event_returns_both_events(client):
    body = client.get("/backtest").json()
    assert set(body["events"]) == {"2017", "2023"}


def test_backtest_unknown_event_is_404(client):
    assert client.get("/backtest?event=1999").status_code == 404


def test_backtest_post_recomputes_top_k_from_the_stored_district_table(client):
    body = client.post("/backtest?event=2017", json={"top_k_pcts": [34, 100]}).json()
    capture = body["models"]["willay_static"]["top_k_capture"]
    assert capture["100"] == 1.0
    assert capture["34"] == 1.0  # the single emergency district is the top-scored one


# --- composition --------------------------------------------------------------------


def test_missing_data_dir_fails_loudly_in_real_mode(tmp_path):
    with pytest.raises(DataDirError):
        real_dependencies(DataDir(tmp_path))


def test_env_flag_selects_the_synthetic_fixtures(monkeypatch):
    monkeypatch.setenv("WILLAY_DATA_MODE", "synthetic")
    assert default_dependencies().synthetic is True


def test_unknown_data_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("WILLAY_DATA_MODE", "maybe")
    with pytest.raises(ValueError):
        default_dependencies()

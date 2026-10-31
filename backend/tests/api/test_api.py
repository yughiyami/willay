import pytest
from fastapi.testclient import TestClient

from willay.api.app import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["synthetic_data"] is True


def test_openapi_schema_is_served(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in ("/health", "/sites", "/sites/{site_id}", "/alerts/evaluate", "/backtest"):
        assert path in paths


# --- GET /sites -------------------------------------------------------------


def test_sites_are_ranked_with_factor_breakdown(client):
    response = client.get("/sites")
    assert response.status_code == 200
    body = response.json()
    assert body["synthetic"] is True
    assert "SYNTHETIC" in body["disclaimer"]
    sites = body["sites"]
    assert 3 <= len(sites) <= 5
    values = [s["risk"]["value"] for s in sites]
    assert values == sorted(values, reverse=True)
    assert [s["rank"] for s in sites] == list(range(1, len(sites) + 1))
    for site in sites:
        assert site["synthetic"] is True
        assert set(site["risk"]["factors"]) == {
            "trigger",
            "susceptibility",
            "tailings_vulnerability",
            "downstream_exposure",
        }
        for factor in site["risk"]["factors"].values():
            assert 0.0 <= factor["value"] <= 1.0
        assert 0.0 <= site["risk"]["value"] <= 1.0
        assert "lon" in site and "lat" in site


def test_sites_include_the_current_hazard_scenario(client):
    body = client.get("/sites").json()
    assert body["hazard_context"]["enfen_magnitude"] == "strong"


# --- GET /sites/{id} ----------------------------------------------------------


def test_site_detail_has_exposure_and_current_tier(client):
    response = client.get("/sites/syn-001")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "syn-001"
    assert body["exposure"]["settlements"]
    assert body["alert"]["tier"] in {"NONE", "PREPARATION", "ATTENTION", "PENDING_CONFIRMATION", "EVACUATION"}
    assert body["alert"]["tier"] != "EVACUATION"  # operator confirmation is never implied


def test_site_detail_unknown_id_is_404(client):
    assert client.get("/sites/does-not-exist").status_code == 404


# --- POST /alerts/evaluate ------------------------------------------------------


def evaluate(client, **payload):
    return client.post("/alerts/evaluate", json=payload)


def test_evaluate_without_confirmation_is_pending(client):
    response = evaluate(client, site_static_risk=0.9, observed_rain_24h_mm=30, water_rise_cm=45)
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "PENDING_CONFIRMATION"
    assert body["requires_operator_confirmation"] is True


def test_evaluate_with_confirmation_is_evacuation(client):
    response = evaluate(
        client, site_static_risk=0.9, observed_rain_24h_mm=30, water_rise_cm=45, operator_confirmed=True
    )
    assert response.json()["tier"] == "EVACUATION"
    assert response.json()["requires_operator_confirmation"] is False


def test_evaluate_preparation(client):
    response = evaluate(
        client,
        site_static_risk=0.7,
        enfen_magnitude="strong",
        seasonal_rainfall_anomaly="above_normal",
    )
    assert response.json()["tier"] == "PREPARATION"


def test_evaluate_attention_from_senamhi_warning(client):
    assert evaluate(client, site_static_risk=0.0, senamhi_level="orange").json()["tier"] == "ATTENTION"


def test_evaluate_custom_thresholds(client):
    response = evaluate(
        client, site_static_risk=0.0, forecast_rain_72h_mm=40, thresholds={"attention_rain_72h_mm": 30}
    )
    assert response.json()["tier"] == "ATTENTION"


def test_evaluate_by_site_id_returns_message_for_actionable_tier(client):
    response = evaluate(client, site_id="syn-001", senamhi_level="red")
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "ATTENTION"
    assert body["site_static_risk"] > 0
    message = body["message"]
    assert message["tier"] == "ATTENTION"
    assert "SYNTHETIC Tailings Alpha" in message["text"]
    assert "no las reemplaza" in message["text"]  # household messages default to Spanish
    assert message["language"] == "es"
    assert message["rephrased"] is False


def test_evaluate_by_site_id_pending_message_is_operator_only(client):
    response = evaluate(client, site_id="syn-001", observed_rain_24h_mm=40, watcher_report=True)
    body = response.json()
    assert body["tier"] == "PENDING_CONFIRMATION"
    assert body["message"]["audience"] == "operator"


def test_evaluate_none_tier_has_no_message(client):
    body = evaluate(client, site_id="syn-003").json()
    assert body["tier"] == "NONE"
    assert body["message"] is None


def test_evaluate_requires_site_id_or_static_risk(client):
    assert evaluate(client, senamhi_level="red").status_code == 422


def test_evaluate_unknown_site_is_404(client):
    assert evaluate(client, site_id="nope").status_code == 404


def test_evaluate_rejects_invalid_enum_and_range(client):
    assert evaluate(client, site_static_risk=0.5, enfen_magnitude="catastrophic").status_code == 422
    assert evaluate(client, site_static_risk=1.5).status_code == 422
    assert evaluate(client, site_static_risk=0.5, observed_rain_24h_mm=-1).status_code == 422


# --- POST /backtest ---------------------------------------------------------------


def test_backtest_runs_on_synthetic_fixture(client):
    response = client.post("/backtest")
    assert response.status_code == 200
    body = response.json()
    assert body["synthetic"] is True
    assert 0.0 <= body["auc"] <= 1.0
    assert set(body["top_k_capture"]) == {"10", "20"}
    assert body["lead_time_days"]["n"] >= 1
    assert body["lead_time_days"]["median"] is not None
    assert 0.0 <= body["evacuation_false_alarm_rate"] <= 1.0


def test_backtest_accepts_custom_top_k(client):
    response = client.post("/backtest", json={"top_k_pcts": [25, 50]})
    assert response.status_code == 200
    assert set(response.json()["top_k_capture"]) == {"25", "50"}


def test_backtest_rejects_invalid_top_k(client):
    assert client.post("/backtest", json={"top_k_pcts": [0]}).status_code == 422
    assert client.post("/backtest", json={"top_k_pcts": [101]}).status_code == 422


# --- CORS for the local web app ------------------------------------------------------


def test_cors_allows_local_vite_origin(client):
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_evaluate_message_can_be_requested_in_english(client):
    body = client.post(
        "/alerts/evaluate",
        json={"site_id": client.get("/sites").json()["sites"][0]["id"], "senamhi_level": "orange", "language": "en"},
    ).json()
    assert "does not replace" in body["message"]["text"]
    assert body["message"]["language"] == "en"


def test_quechua_is_reported_not_available_without_a_human_reviewed_template(client):
    response = client.post(
        "/alerts/evaluate",
        json={"site_id": client.get("/sites").json()["sites"][0]["id"], "senamhi_level": "orange", "language": "qu"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "ATTENTION"  # the tier is still decided deterministically
    assert body["message"]["available"] is False
    assert body["message"]["language"] == "qu"
    assert "human-reviewed" in body["message"]["reason"]

"""POST /alerts/dispatch, GET /alerts/cases, GET /alerts/households, POST /alerts/{id}/response."""

import json

import pytest
from fastapi.testclient import TestClient

from willay.adapters.asterisk_ari import AsteriskAriAdapter
from willay.adapters.households_fixture import ENV_HOUSEHOLDS_FILE, load_households
from willay.adapters.simulated_call import SimulatedCallAdapter
from willay.api.app import create_app
from willay.api.dependencies import attach_calls_from_env, synthetic_dependencies


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(synthetic_dependencies()))


def dispatch(client, tier="ATTENTION", **extra):
    return client.post("/alerts/dispatch", json={"site_id": "syn-001", "tier": tier, **extra})


# --- dispatch ---------------------------------------------------------------------


def test_dispatch_messages_and_calls_the_synthetic_households(client):
    response = dispatch(client)
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "ATTENTION" and body["site_id"] == "syn-001"
    assert body["households_targeted"] == len(body["alerts"]) >= 3
    first = body["alerts"][0]
    assert first["message_status"] == "sent" and first["call_status"] == "completed"
    assert "lluvias" in first["message_text"] and first["response"] in ("SAFE", "NEEDS_HELP", "NO_ANSWER")
    assert body["synthetic_households"] is True


def test_phone_numbers_are_masked_in_every_response(client):
    households = load_households()
    body = json.dumps([dispatch(client).json(), client.get("/alerts/households").json(), client.get("/alerts/cases").json()])
    assert all(h.phone not in body for h in households)


def test_evacuation_needs_an_operator_confirmation_id(client):
    assert dispatch(client, "EVACUATION").status_code == 422
    assert dispatch(client, "EVACUATION", operator_confirmation_id="  ").status_code == 422
    assert client.get("/alerts/households").json()["alerts"] == []  # nothing went out
    ok = dispatch(client, "EVACUATION", operator_confirmation_id="OP-2026-0007")
    assert ok.status_code == 200 and ok.json()["operator_confirmation_id"] == "OP-2026-0007"


@pytest.mark.parametrize("tier", ["NONE", "PENDING_CONFIRMATION", "BANANA"])
def test_only_household_tiers_can_be_dispatched(client, tier):
    assert dispatch(client, tier, operator_confirmation_id="OP-1").status_code == 422


def test_unknown_site_is_404(client):
    response = client.post("/alerts/dispatch", json={"site_id": "nope", "tier": "ATTENTION"})
    assert response.status_code == 404


def test_preparation_only_messages(client):
    body = dispatch(client, "PREPARATION").json()
    assert all(a["call_status"] == "not_attempted" and a["response"] is None for a in body["alerts"])
    assert body["cases"] == []


# --- cases and statuses -----------------------------------------------------------------


def test_needs_help_and_no_answer_become_operator_cases(client):
    body = dispatch(client).json()
    expected = {a["id"] for a in body["alerts"] if a["response"] in ("NEEDS_HELP", "NO_ANSWER")}
    cases = client.get("/alerts/cases").json()["cases"]
    assert {c["alert_id"] for c in cases} == expected and expected
    assert all(c["status"] == "open" and c["reason"] in ("NEEDS_HELP", "NO_ANSWER") for c in cases)
    assert all(c["household_label"] and c["settlement_name"] for c in cases)
    assert client.get("/alerts/cases?status=closed").json()["cases"] == []


def test_households_endpoint_lists_status_message_and_the_location_for_the_globe(client):
    dispatch(client)
    alerts = client.get("/alerts/households").json()["alerts"]
    assert alerts and all({"lon", "lat", "message_text", "response", "household_label"} <= set(a) for a in alerts)
    counts = client.get("/alerts/households").json()["counts"]
    assert sum(counts.values()) == len(alerts)


def test_a_whatsapp_style_reply_updates_the_alert_and_closes_the_case(client):
    dispatch(client)
    case = client.get("/alerts/cases").json()["cases"][0]
    reply = client.post(f"/alerts/{case['alert_id']}/response", json={"response": "SAFE", "channel": "whatsapp"})
    assert reply.status_code == 200 and reply.json()["alert"]["response"] == "SAFE"
    assert reply.json()["alert"]["response_channel"] == "whatsapp"
    closed = [c for c in client.get("/alerts/cases").json()["cases"] if c["id"] == case["id"]][0]
    assert closed["status"] == "closed"


def test_a_needs_help_reply_opens_a_case(client):
    body = dispatch(client, "PREPARATION").json()
    alert_id = body["alerts"][0]["id"]
    client.post(f"/alerts/{alert_id}/response", json={"response": "NEEDS_HELP"})
    assert [c["alert_id"] for c in client.get("/alerts/cases").json()["cases"]] == [alert_id]


def test_reply_validation(client):
    dispatch(client)
    alert_id = client.get("/alerts/households").json()["alerts"][0]["id"]
    assert client.post(f"/alerts/{alert_id}/response", json={"response": "MAYBE"}).status_code == 422
    assert client.post("/alerts/alert-9999/response", json={"response": "SAFE"}).status_code == 404


# --- telephony wiring --------------------------------------------------------------------


def test_calls_are_simulated_by_default(client):
    assert client.get("/health").json()["calls_mode"] == "simulated"


def test_no_calls_unless_explicitly_enabled(monkeypatch):
    deps = attach_calls_from_env(synthetic_dependencies(), {})
    assert isinstance(deps.calls, SimulatedCallAdapter)
    deps = attach_calls_from_env(synthetic_dependencies(), {"WILLAY_CALLS_ENABLED": "0"})
    assert isinstance(deps.calls, SimulatedCallAdapter)


def test_enabled_but_unconfigured_stays_simulated_and_names_the_missing_variables_only():
    deps = attach_calls_from_env(synthetic_dependencies(), {"WILLAY_CALLS_ENABLED": "1", "ASTERISK_ARI_USER": "u"})
    assert isinstance(deps.calls, SimulatedCallAdapter)
    assert "ASTERISK_ARI_URL" in deps.calls_mode and "ASTERISK_ARI_PASSWORD" in deps.calls_mode


def test_enabled_and_configured_uses_asterisk():
    env = {
        "WILLAY_CALLS_ENABLED": "1",
        "ASTERISK_ARI_URL": "http://asterisk.test:8088",
        "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "never-shown",
        "ASTERISK_TRUNK_NAME": "my-trunk",
        "WILLAY_CALL_ALLOWLIST": "+51999000111",
    }
    deps = attach_calls_from_env(synthetic_dependencies(), env)
    assert isinstance(deps.calls, AsteriskAriAdapter)
    assert deps.calls_mode.startswith("asterisk") and "never-shown" not in deps.calls_mode


def test_with_real_calls_fixture_numbers_outside_the_allowlist_are_blocked_not_dialled():
    env = {
        "WILLAY_CALLS_ENABLED": "1",
        "ASTERISK_ARI_URL": "http://asterisk.test:8088",
        "ASTERISK_ARI_USER": "u",
        "ASTERISK_ARI_PASSWORD": "p",
        "ASTERISK_TRUNK_NAME": "my-trunk",
        "WILLAY_CALL_ALLOWLIST": "+51999000111",
    }
    client = TestClient(create_app(attach_calls_from_env(synthetic_dependencies(), env)))
    body = dispatch(client).json()
    assert all(a["call_status"] == "blocked" for a in body["alerts"])


# --- the households fixture --------------------------------------------------------------


def test_the_fixture_households_are_synthetic_and_unique():
    households = load_households()
    assert households and len({h.id for h in households}) == len(households)
    assert len({h.phone for h in households}) == len(households)
    assert all(h.phone.startswith("+") for h in households)


def test_a_local_households_file_can_replace_the_fixture(tmp_path, monkeypatch):
    path = tmp_path / "mine.json"
    path.write_text(
        json.dumps({"households": [{"id": "me", "settlement_id": "set-001", "label": "Yo", "phone": "+51999000111"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_HOUSEHOLDS_FILE, str(path))
    assert [h.id for h in load_households()] == ["me"]

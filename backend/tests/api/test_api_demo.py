"""Demo mode: the scripted end-to-end scenario, offline from a recording and with the same orchestrator code."""

import base64
import time

import pytest
from fastapi.testclient import TestClient

from tests.unit.orchestration_fixtures import SPARK, WONDER, ScriptedByModel, turn
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies

SITE = "$SITE_ID"


def rec(agent, model, text="", *calls, **extra):
    return {
        "agent": agent, "model": model, "text": text, "input_tokens": 800, "output_tokens": 120,
        "tool_calls": [{"name": n, "arguments": a} for n, a in calls], **extra,
    }


def synthetic_recording():
    p, s = (lambda *a, **k: rec("planner", WONDER, *a, **k)), SPARK
    return {
        "recorded_with": {"data_mode": "synthetic-test"},
        "calls": [
            p("", ("ask_hazard_analyst", {"task": "ranking"})),
            rec("hazard_analyst", s, "", ("rank_sites", {"limit": 3}), ("evaluate_alert", {"site_id": SITE})),
            rec("hazard_analyst", s, "El relave Bravo encabeza el ranking; nivel PREPARATION."),
            p("Relave prioritario en PREPARATION."),
            p("", ("ask_hazard_analyst", {"task": "nivel"})),
            rec("hazard_analyst", s, "", ("evaluate_alert", {"site_id": SITE})),
            rec("hazard_analyst", s, "Sube a ATTENTION por el aviso naranja."),
            p("Nivel ATTENTION."),
            p("", ("ask_watcher", {"task": "mide", "clip_id": "demo-clip", "marker_height_cm": 100}),
              ("ask_dispatcher", {"task": "propuesta", "site_id": SITE}), ("ask_communicator", {"task": "mensaje", "site_id": SITE})),
            rec("watcher", s, "", ("measure_clip", {"clip_id": "demo-clip", "marker_height_cm": 100})),
            rec("watcher", s, "El rio subio."),
            rec("dispatcher", s, "", ("prepare_dispatch", {"site_id": SITE})),
            rec("dispatcher", s, "Propuesta lista para el operador."),
            rec("communicator", s, "", ("compose_message", {"site_id": SITE}), ("generate_card", {"site_id": SITE})),
            rec("communicator", s, "Mensaje y ficha listos."),
            p("PENDING_CONFIRMATION: falta la confirmacion del operador."),
        ],
        "card_png_base64": base64.b64encode(b"\x89PNG-demo-card").decode(),
    }


def demo_client(recording="default", **deps_overrides):
    deps = synthetic_dependencies()
    deps.demo_recording = synthetic_recording() if recording == "default" else recording
    deps.demo_pace_s = 0.0
    deps.demo_confirm_timeout_s = 20.0
    for key, value in deps_overrides.items():
        setattr(deps, key, value)
    return TestClient(create_app(deps)), deps


def wait_for(client, run_id, status="completed", seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        trace = client.get(f"/agent/trace/{run_id}").json()
        if trace["status"] != "running":
            return trace
        time.sleep(0.05)
    raise AssertionError("the demo did not finish")


def wait_for_proposal(client, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        proposals = client.get("/agent/proposals").json()["proposals"]
        if proposals:
            return proposals[0]
        time.sleep(0.05)
    raise AssertionError("no proposal appeared")


def steps(trace):
    return [e for e in trace["events"] if e["kind"] == "step"]


def test_status_reports_which_modes_are_available():
    client, _ = demo_client()
    body = client.get("/demo/status").json()
    assert body["offline_available"] is True and body["live_available"] is False and body["running"] is False
    assert demo_client(recording=None)[0].get("/demo/status").json()["offline_available"] is False


def test_the_offline_demo_runs_the_whole_chain_with_the_simulated_operator_click():
    client, _ = demo_client()
    run = client.post("/demo/run", json={"mode": "offline", "auto_confirm": True}).json()
    assert run["mode"] == "offline"
    trace = wait_for(client, run["run_id"])
    assert trace["status"] == "completed", trace["result"]
    titles = [s["detail"]["key"] for s in steps(trace)]
    assert titles == ["start", "enfen", "senamhi", "watcher", "operator", "dispatch", "household_reply", "done"]

    # tiers shown to the audience come from the rules at each stage
    evaluated = [e["result_summary"] for e in trace["events"] if e["kind"] == "tool_call" and e["tool"] == "evaluate_alert"]
    assert "tier=PREPARATION site=syn-002" in evaluated[0] and "tier=ATTENTION site=syn-002" in evaluated[1]
    prepared = [e for e in trace["events"] if e["kind"] == "tool_call" and e["tool"] == "prepare_dispatch"]
    assert "tier=EVACUATION" in prepared[0]["result_summary"]

    # nothing is billed offline, and recorded calls are marked as such
    assert trace["totals"]["cost_usd"] == 0
    model_calls = [e for e in trace["events"] if e["kind"] == "model_call"]
    assert {WONDER, SPARK} <= {e["model"] for e in model_calls} and all(e["detail"].get("replayed") for e in model_calls if e["agent"] != "watcher")
    assert any(e["detail"].get("standin") or e["detail"].get("replayed") for e in model_calls if e["model"].startswith("sam"))

    # the operator click was simulated and says so; the proposal ended dispatched as EVACUATION
    clicks = [e for e in trace["events"] if e["kind"] == "operator_action"]
    assert clicks and "simulated" in clicks[0]["result_summary"]
    (proposal,) = client.get("/agent/proposals").json()["proposals"]
    assert proposal["status"] == "dispatched" and proposal["final_tier"] == "EVACUATION"

    # a household answered 2 and an operator case is open
    households = client.get("/alerts/households").json()
    assert households["counts"]["NEEDS_HELP"] >= 1
    assert [c for c in client.get("/alerts/cases").json()["cases"] if c["status"] == "open"]
    assert {"card.png"} <= set(trace["artifacts"])


def test_without_auto_confirm_nothing_is_sent_until_the_operator_clicks():
    client, _ = demo_client()
    run = client.post("/demo/run", json={"mode": "offline"}).json()
    proposal = wait_for_proposal(client)
    assert proposal["status"] == "awaiting_operator" and proposal["evaluated_tier"] == "PENDING_CONFIRMATION"
    time.sleep(0.3)
    assert client.get("/alerts/households").json()["alerts"] == []
    assert client.get(f"/agent/trace/{run['run_id']}").json()["status"] == "running"

    assert client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-REAL-1"}).status_code == 200
    trace = wait_for(client, run["run_id"])
    assert trace["status"] == "completed"
    assert not [e for e in trace["events"] if e["kind"] == "operator_action" and "simulated" in e["result_summary"]]
    assert client.get("/alerts/cases").json()["cases"]


def test_if_the_operator_never_clicks_the_demo_ends_and_sends_nothing():
    client, _ = demo_client(demo_confirm_timeout_s=0.3)
    run = client.post("/demo/run", json={"mode": "offline"}).json()
    trace = wait_for(client, run["run_id"])
    assert trace["status"] == "completed" and "operator_timeout" in str(trace["result"])
    assert client.get("/alerts/households").json()["alerts"] == []


def test_starting_the_demo_clears_the_previous_demo_state():
    client, _ = demo_client()
    first = client.post("/demo/run", json={"mode": "offline", "auto_confirm": True}).json()
    wait_for(client, first["run_id"])
    second = client.post("/demo/run", json={"mode": "offline", "auto_confirm": True}).json()
    wait_for(client, second["run_id"])
    assert len(client.get("/agent/proposals").json()["proposals"]) == 1
    assert len({a["dispatch_id"] for a in client.get("/alerts/households").json()["alerts"]}) == 1


def test_only_one_demo_runs_at_a_time():
    client, _ = demo_client()
    client.post("/demo/run", json={"mode": "offline"})
    assert client.post("/demo/run", json={"mode": "offline"}).status_code == 409
    proposal = wait_for_proposal(client)
    client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-1"})


def test_offline_without_a_recording_is_a_clear_error():
    client, _ = demo_client(recording=None)
    response = client.post("/demo/run", json={"mode": "offline"})
    assert response.status_code == 503 and "record_demo" in response.json()["detail"]


def test_live_mode_needs_the_meta_models():
    client, _ = demo_client()
    assert client.post("/demo/run", json={"mode": "live"}).status_code == 503


def test_a_recording_that_no_longer_matches_fails_the_run_with_a_clear_message():
    broken = synthetic_recording()
    broken["calls"] = broken["calls"][:2]  # truncated: the third model call has nothing to replay
    client, _ = demo_client(recording=broken)
    run = client.post("/demo/run", json={"mode": "offline", "auto_confirm": True}).json()
    trace = wait_for(client, run["run_id"])
    assert trace["status"] == "failed" and "recording" in trace["result"]["error"]


def test_live_mode_runs_the_same_script_against_the_meta_models_and_bills_them():
    scripted = ScriptedByModel(
        {
            WONDER: [
                turn("", model=WONDER),  # placeholder replaced below
            ]
        }
    )
    client, deps = demo_client()
    scripted.scripts = {
        WONDER: [
            turn("Estado general: sin novedades.", model=WONDER),
            turn("Sin cambios.", model=WONDER),
            turn("No se pudo preparar una propuesta.", model=WONDER),
        ],
        SPARK: [],
    }
    deps.agent_model = scripted
    run = client.post("/demo/run", json={"mode": "live", "auto_confirm": True}).json()
    trace = wait_for(client, run["run_id"])
    # the scripted planner never prepares a proposal, so the run stops there honestly instead of faking the rest
    assert trace["status"] == "failed" and "proposal" in trace["result"]["error"]
    assert trace["totals"]["cost_usd"] > 0
    assert not any(e["detail"].get("replayed") for e in trace["events"] if e["kind"] == "model_call")

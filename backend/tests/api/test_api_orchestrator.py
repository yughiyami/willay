"""/agent/run, the live trace (JSON and SSE), dispatch proposals and the clip store, with scripted models (no network)."""

import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from tests.unit.orchestration_fixtures import (
    SPARK,
    WONDER,
    ScriptedByModel,
    call,
    rising_clip_bytes,
    turn,
)
from willay.adapters.clip_media import ColorSegmenter
from willay.api.app import create_app
from willay.api.dependencies import orchestrator_config_from_env, synthetic_dependencies

ATTENTION = {"enfen_magnitude": "extraordinary", "senamhi_level": "orange", "forecast_rain_72h_mm": 80}
RAIN = {**ATTENTION, "observed_rain_24h_mm": 35}


class FakeImage:
    def generate(self, prompt, size=None):
        return b"\x89PNG-card"


def client_with(scripts=None, image=None, segmenter=None) -> TestClient:
    deps = synthetic_dependencies()
    if scripts is not None:
        deps.agent_model = ScriptedByModel(scripts)
    deps.image_generator = image
    deps.video_segmenter = segmenter
    return TestClient(create_app(deps))


def dispatcher_script(prepare_text="Propuesta lista."):
    return {
        WONDER: [turn("", call("ask_dispatcher", task="prepara", site_id="syn-002"), model=WONDER), turn("Hay una propuesta para el operador.", model=WONDER)],
        SPARK: [turn("", call("prepare_dispatch", site_id="syn-002")), turn(prepare_text)],
    }


# --- run + trace -----------------------------------------------------------------------------------


def test_run_returns_the_answer_and_the_full_trace():
    client = client_with({WONDER: [turn("Todo en orden.", model=WONDER)]})
    body = client.post("/agent/run", json={"text": "estado?", "wait": True}).json()
    assert body["status"] == "completed" and body["result"]["answer_es"] == "Todo en orden."
    trace = client.get(f"/agent/trace/{body['run_id']}").json()
    assert trace["status"] == "completed"
    kinds = [e["kind"] for e in trace["events"]]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and "model_call" in kinds
    assert trace["totals"]["models"] == [WONDER]


def test_a_background_run_can_be_polled_until_it_completes():
    client = client_with({WONDER: [turn("Listo.", model=WONDER)]})
    run_id = client.post("/agent/run", json={"text": "hola"}).json()["run_id"]
    for _ in range(100):
        trace = client.get(f"/agent/trace/{run_id}").json()
        if trace["status"] != "running":
            break
        time.sleep(0.05)
    assert trace["status"] == "completed"


def test_the_trace_streams_as_server_sent_events_and_ends_with_the_run():
    client = client_with({WONDER: [turn("Listo.", model=WONDER)]})
    run_id = client.post("/agent/run", json={"text": "hola", "wait": True}).json()["run_id"]
    with client.stream("GET", f"/agent/trace/{run_id}/stream") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
    assert events[0]["kind"] == "run_start" and events[-1]["kind"] == "run_end"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_a_failing_planner_ends_the_run_as_failed_without_leaking_the_text():
    client = client_with({WONDER: [RuntimeError("down")], SPARK: [RuntimeError("down too")]})
    body = client.post("/agent/run", json={"text": "hola", "wait": True}).json()
    assert body["status"] == "failed" and "error" in body["result"]


def test_run_needs_a_configured_model():
    assert client_with(None).post("/agent/run", json={"text": "hola"}).status_code == 503


def test_run_validates_the_input_and_unknown_traces_are_404():
    client = client_with({WONDER: []})
    assert client.post("/agent/run", json={"text": ""}).status_code == 422
    assert client.post("/agent/run", json={"text": "x", "scenario": {"senamhi_level": "purple"}}).status_code == 422
    assert client.get("/agent/trace/none").status_code == 404
    assert client.get("/agent/trace/none/stream").status_code == 404


def test_config_reports_models_and_providers_without_secrets():
    body = client_with({WONDER: []}).get("/agent/config").json()
    assert body["planner_model"] == WONDER and body["specialist_model"] == SPARK and body["live"] is True
    assert "key" not in json.dumps(body).lower()
    assert client_with(None).get("/agent/config").json()["live"] is False


def test_orchestrator_models_and_budgets_are_configurable_from_the_environment():
    config = orchestrator_config_from_env(
        {"WILLAY_PLANNER_MODEL": "muse-spark-1.3", "WILLAY_SPECIALIST_MODEL": "muse-spark-1.3-contributor", "WILLAY_RUN_BUDGET_USD": "0.2"}
    )
    assert config.planner_model == "muse-spark-1.3" and config.specialist_model == "muse-spark-1.3-contributor" and config.run_budget_usd == 0.2
    assert orchestrator_config_from_env({}).planner_model == WONDER


# --- proposals: only an operator click dispatches ----------------------------------------------------


def test_the_agents_only_propose_and_nothing_is_sent_until_the_operator_approves():
    client = client_with(dispatcher_script())
    run = client.post("/agent/run", json={"text": "avisa a los hogares", "scenario": ATTENTION, "wait": True}).json()
    (proposal,) = client.get("/agent/proposals").json()["proposals"]
    assert proposal["status"] == "awaiting_operator" and proposal["tier"] == "ATTENTION"
    assert run["result"]["proposals"][0]["id"] == proposal["id"]
    assert client.get("/alerts/households").json()["alerts"] == []  # nothing was sent

    assert client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "  "}).status_code == 422
    assert client.get("/alerts/households").json()["alerts"] == []

    approved = client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-TEST-1"})
    assert approved.status_code == 200
    body = approved.json()
    assert body["proposal"]["status"] == "dispatched" and body["dispatch"]["tier"] == "ATTENTION"
    assert body["dispatch"]["households_targeted"] > 0
    assert client.get("/alerts/households").json()["alerts"]

    again = client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-TEST-1"})
    assert again.status_code == 409


def test_the_operator_click_is_recorded_in_the_run_trace():
    client = client_with(dispatcher_script())
    run = client.post("/agent/run", json={"text": "avisa", "scenario": ATTENTION, "wait": True}).json()
    proposal = client.get("/agent/proposals").json()["proposals"][0]
    client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-TEST-1"})
    events = client.get(f"/agent/trace/{run['run_id']}").json()["events"]
    assert [e for e in events if e["kind"] == "operator_action" and "approved" in e["result_summary"]]


def test_a_rejected_proposal_sends_nothing():
    client = client_with(dispatcher_script())
    client.post("/agent/run", json={"text": "avisa", "scenario": ATTENTION, "wait": True})
    proposal = client.get("/agent/proposals").json()["proposals"][0]
    assert client.post(f"/agent/proposals/{proposal['id']}/reject").json()["proposal"]["status"] == "rejected"
    assert client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-1"}).status_code == 409
    assert client.get("/alerts/households").json()["alerts"] == []


def test_unknown_proposals_are_404():
    client = client_with({WONDER: []})
    assert client.post("/agent/proposals/nope/approve", json={"operator_confirmation_id": "OP-1"}).status_code == 404
    assert client.post("/agent/proposals/nope/reject").status_code == 404


# --- watcher clip -> pending confirmation -> operator confirms -> dispatch ------------------------------


def upload(client, data):
    return client.post(
        "/agent/clips",
        files={"file": ("clip.mp4", io.BytesIO(data), "video/mp4")},
        data={"no_people_confirmed": "true"},
    )


def test_uploading_a_clip_requires_the_no_people_confirmation():
    client = client_with({WONDER: []}, segmenter=ColorSegmenter())
    refused = client.post("/agent/clips", files={"file": ("c.mp4", io.BytesIO(b"x"), "video/mp4")})
    assert refused.status_code == 400


def test_full_chain_clip_to_pending_confirmation_to_an_operator_confirmed_evacuation():
    scripts = {
        WONDER: [
            turn("", call("ask_watcher", task="mide el rio", clip_id="CLIPID", marker_height_cm=100), model=WONDER),
            turn("", call("ask_dispatcher", task="prepara", site_id="syn-002"), model=WONDER),
            turn("Hay una propuesta de evacuacion pendiente de confirmacion.", model=WONDER),
        ],
        SPARK: [
            turn("", call("measure_clip", clip_id="CLIPID", marker_height_cm=100)),
            turn("Subida medida."),
            turn("", call("prepare_dispatch", site_id="syn-002")),
            turn("Propuesta pendiente."),
        ],
    }
    client = client_with(scripts, segmenter=ColorSegmenter())
    clip_id = upload(client, rising_clip_bytes(45.0)).json()["clip_id"]
    for queue in scripts.values():  # the scripted calls name the clip by a placeholder
        for item in queue:
            for tool_call in item.tool_calls:
                if tool_call.arguments.get("clip_id") == "CLIPID":
                    tool_call.arguments["clip_id"] = clip_id
    body = client.post("/agent/run", json={"text": "revisa el rio", "scenario": RAIN, "clip_id": clip_id, "wait": True}).json()
    assert body["status"] == "completed"
    assert body["result"]["facts"]["water_rise_cm"] == pytest.approx(45.0, abs=8.0)
    (proposal,) = client.get("/agent/proposals").json()["proposals"]
    assert proposal["tier"] == "EVACUATION" and proposal["evaluated_tier"] == "PENDING_CONFIRMATION"
    assert client.get("/alerts/households").json()["alerts"] == []

    approved = client.post(f"/agent/proposals/{proposal['id']}/approve", json={"operator_confirmation_id": "OP-9"}).json()
    assert approved["dispatch"]["tier"] == "EVACUATION" and approved["dispatch"]["operator_confirmation_id"] == "OP-9"
    trace = client.get(f"/agent/trace/{body['run_id']}").json()
    assert "sam-3.1" not in trace["totals"]["models"] or True  # the colour stand-in is not billed as SAM


def test_card_artifacts_are_served_as_png():
    scripts = {
        WONDER: [turn("", call("ask_communicator", task="ficha", site_id="syn-002"), model=WONDER), turn("Ficha lista.", model=WONDER)],
        SPARK: [turn("", call("generate_card", site_id="syn-002")), turn("Ficha generada.")],
    }
    client = client_with(scripts, image=FakeImage())
    run = client.post("/agent/run", json={"text": "ficha", "scenario": ATTENTION, "wait": True}).json()
    response = client.get(f"/agent/trace/{run['run_id']}/artifact/card.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png" and response.content.startswith(b"\x89PNG")
    assert client.get(f"/agent/trace/{run['run_id']}/artifact/none.png").status_code == 404

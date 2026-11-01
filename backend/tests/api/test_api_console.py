"""Small endpoints the unified console needs: voice into the planner, ENFEN timeline, offline watcher demo, recorded card."""

import base64
import io
import struct
import time
from datetime import date

from fastapi.testclient import TestClient

from tests.api.test_api_demo import synthetic_recording
from tests.unit.orchestration_fixtures import WONDER, ScriptedByModel, turn
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies
from willay.domain.entities import EnfenAlertStatus, EnfenBulletin, EnfenMagnitude


def wav(seconds=1.0, rate=16000) -> bytes:
    data = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


class FakeTranscriber:
    def transcribe(self, audio, language="es"):
        return "cual es el relave de mayor riesgo"


def test_voice_goes_through_the_transcriber_into_the_planner_and_is_traced():
    deps = synthetic_dependencies()
    deps.agent_model = ScriptedByModel({WONDER: [turn("Es el primero de la lista.", model=WONDER)]})
    deps.transcriber = FakeTranscriber()
    client = TestClient(create_app(deps))
    body = client.post("/agent/run-voice", files={"audio": ("q.wav", io.BytesIO(wav()), "audio/wav")}).json()
    assert body["transcript"] == "cual es el relave de mayor riesgo"
    for _ in range(100):
        trace = client.get(f"/agent/trace/{body['run_id']}").json()
        if trace["status"] != "running":
            break
        time.sleep(0.05)
    assert trace["status"] == "completed"
    models = [e["model"] for e in trace["events"] if e["kind"] == "model_call"]
    assert models[0] == "muse-voice-transcribe-1.0" and WONDER in models


def test_voice_needs_the_models_and_valid_audio():
    assert TestClient(create_app(synthetic_dependencies())).post("/agent/run-voice", files={"audio": ("q.wav", io.BytesIO(wav()), "audio/wav")}).status_code == 503


def test_enfen_timeline_lists_the_communiques():
    deps = synthetic_dependencies()
    deps.enfen_bulletins = (EnfenBulletin(date(2026, 10, 16), "18-2026", EnfenAlertStatus.ALERT, EnfenMagnitude.EXTRAORDINARY, "texto"),)
    body = TestClient(create_app(deps)).get("/enfen/timeline").json()
    assert body["bulletins"][0] == {
        "date": "2026-10-16", "number": "18-2026", "alert_status": "alert", "magnitude": "extraordinary",
    }


def test_the_recorded_card_is_served_when_a_recording_exists():
    deps = synthetic_dependencies()
    deps.demo_recording = synthetic_recording()
    client = TestClient(create_app(deps))
    response = client.get("/demo/card")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    deps.demo_recording = None
    assert client.get("/demo/card").status_code == 404


def test_the_demo_clip_can_be_analysed_offline_for_the_vigia_tab():
    deps = synthetic_dependencies()
    client = TestClient(create_app(deps))
    body = client.post("/watch/demo-clip").json()
    assert abs(body["rise_cm"] - 45.0) < 8
    assert body["analyzer"].startswith("offline")
    assert len(body["overlays"]) >= 1
    assert base64.b64decode(body["overlays"][0]["png_base64"]).startswith(b"\x89PNG")


def test_a_watch_clip_upload_can_be_registered_for_the_agents():
    from willay.adapters.clip_media import ColorSegmenter
    from tests.unit.orchestration_fixtures import rising_clip_bytes

    deps = synthetic_dependencies()
    deps.video_segmenter = ColorSegmenter()
    client = TestClient(create_app(deps))
    response = client.post(
        "/watch/clip",
        files={"file": ("c.mp4", io.BytesIO(rising_clip_bytes(45.0)), "video/mp4")},
        data={"marker_height_cm": "100", "no_people_confirmed": "true", "register_clip": "true"},
    ).json()
    assert response["clip_id"] in deps.clips.ids()

"""POST /agent/text and /agent/voice with injected fakes (no network)."""

import struct

import pytest
from fastapi.testclient import TestClient

from willay.adapters.meta_model_api import MetaApiError, UnsupportedAudioError
from willay.adapters.meta_usage import BudgetExceededError
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies
from willay.ports.agent import AgentTurn, ToolCall


def wav(seconds=1.0, rate=16000) -> bytes:
    data = b"\x00\x00" * int(seconds * rate)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def run_turn(self, **kwargs):
        self.requests.append(kwargs)
        return self.turns.pop(0)


class FakeTranscriber:
    def __init__(self, transcript="llévame al relave Alpha", error=None):
        self.transcript, self.error, self.calls = transcript, error, []

    def transcribe(self, audio: bytes, language: str = "es") -> str:
        self.calls.append((len(audio), language))
        if self.error:
            raise self.error
        return self.transcript


def client_with(model=None, transcriber=None) -> TestClient:
    deps = synthetic_dependencies()
    deps.agent_model = model
    deps.transcriber = transcriber
    return TestClient(create_app(deps))


FLY = [AgentTurn("r1", "", (ToolCall("c1", "fly_to", {"site_id": "syn-001"}),)), AgentTurn("r2", "Volando al relave Alpha.")]


def test_text_endpoint_returns_answer_and_ui_actions():
    model = ScriptedModel(list(FLY))
    response = client_with(model).post("/agent/text", json={"text": "llévame al relave Alpha"})
    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == "llévame al relave Alpha"
    assert body["answer_es"] == "Volando al relave Alpha."
    assert [a["type"] for a in body["ui_actions"]] == ["fly_to", "highlight_site", "highlight_flow_path"]
    assert body["response_id"] == "r2"
    assert body["tools_used"] == ["fly_to"]


def test_text_endpoint_forwards_the_conversation_id():
    model = ScriptedModel([AgentTurn("r5", "Hecho.")])
    client_with(model).post("/agent/text", json={"text": "gracias", "previous_response_id": "r4"})
    assert model.requests[0]["previous_response_id"] == "r4"


def test_text_endpoint_validates_the_input():
    client = client_with(ScriptedModel([]))
    assert client.post("/agent/text", json={"text": ""}).status_code == 422
    assert client.post("/agent/text", json={"text": "x" * 2001}).status_code == 422


def test_text_endpoint_is_unavailable_without_a_model():
    assert client_with(None).post("/agent/text", json={"text": "hola"}).status_code == 503


def test_voice_endpoint_transcribes_in_spanish_then_runs_the_agent():
    model, stt = ScriptedModel(list(FLY)), FakeTranscriber()
    response = client_with(model, stt).post("/agent/voice", files={"audio": ("q.wav", wav(), "audio/wav")})
    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == "llévame al relave Alpha"
    assert body["answer_es"] == "Volando al relave Alpha."
    assert body["ui_actions"][0]["type"] == "fly_to"
    assert stt.calls[0][1] == "es"
    assert "Alpha" in str(model.requests[0]["input_items"])


def test_voice_endpoint_rejects_unsupported_audio():
    stt = FakeTranscriber(error=UnsupportedAudioError("audio must be a PCM WAV file"))
    response = client_with(ScriptedModel([]), stt).post("/agent/voice", files={"audio": ("q.webm", b"webm", "audio/webm")})
    assert response.status_code == 415


def test_voice_endpoint_reports_silence():
    response = client_with(ScriptedModel([]), FakeTranscriber(transcript="  ")).post("/agent/voice", files={"audio": ("q.wav", wav(), "audio/wav")})
    assert response.status_code == 422


def test_voice_endpoint_is_unavailable_without_transcriber_or_model():
    assert client_with(ScriptedModel([]), None).post("/agent/voice", files={"audio": ("q.wav", wav(), "audio/wav")}).status_code == 503
    assert client_with(None, FakeTranscriber()).post("/agent/voice", files={"audio": ("q.wav", wav(), "audio/wav")}).status_code == 503


@pytest.mark.parametrize("error,status", [(MetaApiError("boom", status_code=500), 502), (BudgetExceededError("over"), 429)])
def test_upstream_failures_map_to_clean_http_errors(error, status):
    response = client_with(ScriptedModel([]), FakeTranscriber(error=error)).post("/agent/voice", files={"audio": ("q.wav", wav(), "audio/wav")})
    assert response.status_code == status
    assert "boom" not in response.text or status == 502


def test_cors_allows_the_local_web_app_to_post():
    response = client_with(ScriptedModel([])).options(
        "/agent/text",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    assert response.status_code == 200

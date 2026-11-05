"""Web chat over HTTP: sessions, polling, buttons tied to dispatched alerts, location, voice and media."""

import base64

from fastapi.testclient import TestClient

from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies
from willay.ports.agent import AgentTurn


class FakeModel:
    def run_turn(self, **kwargs):
        return AgentTurn(response_id="r", text="Revise su punto seguro y ordene que debe evacuar ya.")


class FakeConverter:
    def to_wav(self, audio, mime):
        return b"RIFFwav"


class FakeTranscriber:
    def __init__(self, text="estoy a salvo"):
        self.text = text

    def transcribe(self, audio, language="es"):
        return self.text


def make(**fields):
    deps = synthetic_dependencies()
    for name, value in fields.items():
        setattr(deps, name, value)
    return TestClient(create_app(deps)), deps


def session(client, name=None) -> str:
    return client.post("/webchat/sessions", json={"name": name}).json()["session_id"]


def messages(client, sid, after=0):
    return client.get(f"/webchat/sessions/{sid}/messages", params={"after": after}).json()["messages"]


def dispatch(client, tier="ATTENTION", **extra):
    return client.post("/alerts/dispatch", json={"site_id": "syn-001", "tier": tier, **extra}).json()


def households(client) -> dict:
    return {a["id"]: a for a in client.get("/alerts/households").json()["alerts"]}


def test_session_has_a_display_name_and_unknown_sessions_are_404():
    client, _ = make()
    body = client.post("/webchat/sessions", json={"name": "  Rosa   Quispe "}).json()
    assert body["display_name"] == "Rosa Quispe" and body["session_id"]
    assert client.post("/webchat/sessions").json()["display_name"].startswith("Visitante")
    assert client.get("/webchat/sessions/nope/messages").status_code == 404
    assert client.post("/webchat/sessions/nope/messages", json={"type": "text", "text": "hola"}).status_code == 404


def test_hola_gets_the_welcome_and_polling_only_returns_newer_messages():
    client, _ = make()
    sid = session(client)
    assert client.post(f"/webchat/sessions/{sid}/messages", json={"type": "text", "text": "hola"}).json() == {"accepted": True}
    first = messages(client, sid)
    assert [(m["seq"], m["direction"]) for m in first] == [(1, "in"), (2, "out")]
    assert "SENAMHI" in first[1]["text"] and "ENFEN" in first[1]["text"]
    assert set(first[1]) == {"seq", "direction", "kind", "text", "buttons", "image_url", "lat", "lon", "ts", "status"}
    assert messages(client, sid, after=2) == []


def test_the_first_non_greeting_message_also_gets_the_welcome_and_the_guarded_answer():
    client, _ = make(agent_model=FakeModel())
    sid = session(client)
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "text", "text": "Que hago si llueve fuerte?"})
    out = [m["text"] for m in messages(client, sid) if m["direction"] == "out"]
    assert "SENAMHI" in out[0] and len(out) == 2
    assert "evacuar" not in out[1].lower() and "INDECI" in out[1]  # the guardrail replaced an evacuation order


def test_dispatch_reaches_every_chat_and_help_safe_update_the_console():
    client, _ = make()
    first, second = session(client), session(client)
    body = dispatch(client)
    for sid in (first, second):
        alert_message = messages(client, sid)[-1]
        assert alert_message["kind"] == "buttons" and alert_message["direction"] == "out"
        assert [b["title"] for b in alert_message["buttons"]] == ["Estoy a salvo", "Necesito ayuda"]
        assert alert_message["buttons"][0]["id"].startswith("SAFE:alert-")
    ids = {messages(client, sid)[-1]["buttons"][0]["id"].split(":")[1] for sid in (first, second)}
    assert len(ids) == 2 and ids <= {a["id"] for a in body["alerts"]}  # each chat is a different dispatched household

    help_id = messages(client, first)[-1]["buttons"][1]["id"]
    client.post(f"/webchat/sessions/{first}/messages", json={"type": "button", "id": help_id})
    alert_id = help_id.split(":")[1]
    assert households(client)[alert_id]["response"] == "NEEDS_HELP"
    assert households(client)[alert_id]["response_channel"] == "webchat"
    assert any(c["alert_id"] == alert_id and c["status"] == "open" for c in client.get("/alerts/cases").json()["cases"])
    tail = messages(client, first)
    assert tail[-2]["direction"] == "in" and tail[-2]["text"] == "Necesito ayuda"
    assert "operador" in tail[-1]["text"]

    client.post(f"/webchat/sessions/{first}/messages", json={"type": "button", "id": f"SAFE:{alert_id}"})
    assert households(client)[alert_id]["response"] == "SAFE"
    assert all(c["status"] == "closed" for c in client.get("/alerts/cases").json()["cases"] if c["alert_id"] == alert_id)


def test_a_button_from_another_chat_is_refused():
    client, _ = make()
    first, second = session(client), session(client)
    dispatch(client)
    foreign = messages(client, second)[-1]["buttons"][0]["id"]
    client.post(f"/webchat/sessions/{first}/messages", json={"type": "button", "id": foreign})
    assert households(client)[foreign.split(":")[1]]["response_channel"] != "webchat"
    assert "No encontramos esa alerta" in messages(client, first)[-1]["text"]


def test_text_commands_answer_the_bound_alert():
    client, _ = make()
    sid = session(client)
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "text", "text": "2"})
    assert "No encontramos una alerta" in messages(client, sid)[-1]["text"]
    dispatch(client)
    alert_id = messages(client, sid)[-1]["buttons"][0]["id"].split(":")[1]
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "text", "text": "Necesito ayuda"})
    assert households(client)[alert_id]["response"] == "NEEDS_HELP"


def test_location_is_attached_to_the_open_case():
    client, _ = make()
    sid = session(client)
    dispatch(client)
    alert_id = messages(client, sid)[-1]["buttons"][0]["id"].split(":")[1]
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "button", "id": f"SAFE:{alert_id}"})
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "location", "lat": -11.9, "lon": -76.7})
    assert "no hay un caso abierto" in messages(client, sid)[-1]["text"]
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "button", "id": f"HELP:{alert_id}"})
    client.post(f"/webchat/sessions/{sid}/messages", json={"type": "location", "lat": -11.9, "lon": -76.7})
    case = next(c for c in client.get("/alerts/cases").json()["cases"] if c["alert_id"] == alert_id and c["status"] == "open")
    assert (case["reported_lat"], case["reported_lon"]) == (-11.9, -76.7) and "web chat" in case["location_note"]
    shared = [m for m in messages(client, sid) if m["kind"] == "location"]
    assert shared and shared[-1]["lat"] == -11.9


def test_invalid_bodies_are_rejected():
    client, _ = make()
    sid = session(client)
    for body in ({"type": "text", "text": ""}, {"type": "location", "lat": 95, "lon": 0}, {"type": "wat"}, {"type": "button"}):
        assert client.post(f"/webchat/sessions/{sid}/messages", json=body).status_code == 422


def test_voice_note_is_transcribed_and_answered_like_text():
    client, _ = make(audio_converter=FakeConverter(), transcriber=FakeTranscriber("hola"))
    sid = session(client)
    response = client.post(f"/webchat/sessions/{sid}/audio", files={"file": ("voice.webm", b"webm-bytes", "audio/webm")})
    assert response.status_code == 200 and response.json()["transcript"] == "hola"
    tail = messages(client, sid)
    assert tail[0]["kind"] == "audio" and tail[0]["direction"] == "in" and tail[0]["text"] == "hola"
    assert "SENAMHI" in tail[-1]["text"]


def test_voice_note_without_a_transcriber_says_so_and_empty_upload_is_400():
    client, _ = make(audio_converter=None, transcriber=None)
    sid = session(client)
    client.post(f"/webchat/sessions/{sid}/audio", files={"file": ("voice.webm", b"x", "audio/webm")})
    assert "no puedo escuchar" in messages(client, sid)[-1]["text"]
    assert client.post(f"/webchat/sessions/{sid}/audio", files={"file": ("v.webm", b"", "audio/webm")}).status_code == 400


def test_evacuation_card_is_sent_as_an_image_when_one_is_available():
    png = b"\x89PNG\r\n\x1a\nfake"
    client, _ = make(demo_recording={"card_png_base64": base64.b64encode(png).decode()})
    sid = session(client)
    dispatch(client, "EVACUATION", operator_confirmation_id="OP-1")
    image = messages(client, sid)[-1]
    assert image["kind"] == "image" and image["image_url"].startswith("/webchat/media/")
    served = client.get(image["image_url"])
    assert served.status_code == 200 and served.content == png and served.headers["content-type"] == "image/png"
    assert client.get("/webchat/media/unknown").status_code == 404


def test_session_cap_forgets_the_oldest():
    client, deps = make()
    ids = [session(client) for _ in range(22)]
    assert len(deps.webchat.session_ids()) == 20
    assert client.get(f"/webchat/sessions/{ids[0]}/messages").status_code == 404
    assert client.get(f"/webchat/sessions/{ids[-1]}/messages").status_code == 200


def test_health_reports_the_chat_modes_and_cors_allows_the_dev_server():
    client, _ = make()
    health = client.get("/health").json()
    assert health["webchat_mode"].startswith("live") and health["telegram_mode"] == "disabled: TELEGRAM_BOT_TOKEN missing"
    preflight = client.options(
        "/webchat/sessions",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert client.get("/telegram/status").json()["enabled"] is False

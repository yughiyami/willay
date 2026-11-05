"""WhatsApp webhook, operator test endpoint and event log over HTTP, with a fake Graph API (no network)."""

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.unit.test_whatsapp_cloud import ENV as BASE_ENV
from tests.unit.test_whatsapp_cloud import SECRET, TOKEN, VERIFY, FakeGraph
from willay.api.app import create_app
from willay.api.dependencies import attach_messaging_from_env, synthetic_dependencies
from willay.domain.dispatch import ResponseKind
from willay.ports.agent import AgentTurn

ENV = {**BASE_ENV, "WILLAY_MESSAGING_PROVIDER": "whatsapp", "WILLAY_CALL_ALLOWLIST": "+51900000001,+51900000002"}
WA = "51900000001"


class FakeModel:
    def __init__(self):
        self.calls = []

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return AgentTurn(response_id="r", text="Revise su punto seguro.")


class FakeConverter:
    def to_wav(self, audio, mime):
        return b"RIFFwav"


class FakeTranscriber:
    def transcribe(self, audio, language="es"):
        return "estoy a salvo"


def build(env=ENV, graph=None, **fields):
    graph = graph or FakeGraph()
    deps = synthetic_dependencies()
    deps = attach_messaging_from_env(deps, environ=env, http_client=httpx.Client(transport=httpx.MockTransport(graph)))
    for name, value in fields.items():
        setattr(deps, name, value)
    return TestClient(create_app(deps)), deps, graph


def sign(raw: bytes, secret: str = SECRET) -> dict:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}


def post(client, payload, headers=None):
    raw = json.dumps(payload).encode()
    return client.post("/whatsapp/webhook", content=raw, headers=sign(raw) if headers is None else headers)


def inbound(**message):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {"messaging_product": "whatsapp", "messages": [{"from": WA, "id": "wamid.IN1", "timestamp": "1", **message}]}}]}],
    }


def dispatch(client):
    return client.post("/alerts/dispatch", json={"site_id": "syn-001", "tier": "PREPARATION"})


# --- availability ---------------------------------------------------------------------------------------


def test_every_whatsapp_endpoint_is_404_while_the_simulator_is_active():
    client = TestClient(create_app(synthetic_dependencies()))
    assert client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}).status_code == 404
    assert client.post("/whatsapp/webhook", json={}).status_code == 404
    assert client.post("/whatsapp/test", json={"to": "+51900000001"}).status_code == 404
    log = client.get("/whatsapp/log")
    assert log.status_code == 200 and log.json()["events"] == [] and log.json()["messaging_mode"] == "simulated"


def test_health_reports_the_messaging_mode():
    client, _, _ = build()
    assert client.get("/health").json()["messaging_mode"].startswith("whatsapp")
    default = TestClient(create_app(synthetic_dependencies()))
    assert default.get("/health").json()["messaging_mode"] == "simulated"


# --- verification handshake -------------------------------------------------------------------------------


def test_the_verification_handshake_returns_the_challenge_as_plain_text():
    client, _, _ = build()
    response = client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"})
    assert response.status_code == 200 and response.text == "1158201444"
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "params",
    [
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.challenge": "1"},
        {},
    ],
)
def test_a_wrong_verify_token_or_mode_is_403(params):
    client, _, _ = build()
    assert client.get("/whatsapp/webhook", params=params).status_code == 403


# --- signature ------------------------------------------------------------------------------------------------


def test_a_bad_or_missing_signature_is_403_and_nothing_is_processed():
    client, deps, graph = build()
    dispatch(client)
    graph.requests.clear()
    alert = deps.ledger.list_alerts()[0]
    payload = inbound(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": f"SAFE:{alert.id}", "title": "x"}})
    raw = json.dumps(payload).encode()
    assert client.post("/whatsapp/webhook", content=raw, headers=sign(raw, "other-secret")).status_code == 403
    assert client.post("/whatsapp/webhook", content=raw, headers={"Content-Type": "application/json"}).status_code == 403
    assert alert.response is None and graph.requests == []


def test_an_invalid_json_body_with_a_valid_signature_is_400():
    client, _, _ = build()
    raw = b"not json"
    assert client.post("/whatsapp/webhook", content=raw, headers=sign(raw)).status_code == 400


# --- inbound ---------------------------------------------------------------------------------------------------


def first_alert(deps, phone="+51900000001"):
    return next(a for a in deps.ledger.list_alerts() if a.household.phone == phone)


def test_the_dispatch_goes_out_as_text_plus_buttons_and_only_to_allowlisted_numbers():
    client, deps, graph = build(env={**ENV, "WILLAY_CALL_ALLOWLIST": "+51900000001"})
    body = dispatch(client).json()
    statuses = {a["household_id"]: a["message_status"] for a in body["alerts"]}
    assert statuses["hh-001"] == "sent" and all(s == "failed" for k, s in statuses.items() if k != "hh-001")
    sent = [b for b in graph.message_bodies()]
    assert len(sent) == 1 and sent[0]["type"] == "interactive" and sent[0]["to"] == "51900000001"
    ids = [b["reply"]["id"] for b in sent[0]["interactive"]["action"]["buttons"]]
    alert = first_alert(deps)
    assert ids == [f"SAFE:{alert.id}", f"HELP:{alert.id}"] and alert.message_id
    # the phone mock-up still shows what was really sent, and only that
    assert len(deps.messaging.mirror.all_messages()) == 1


def test_a_safe_button_reply_is_recorded_and_confirmed_on_whatsapp_and_in_the_mock_up():
    client, deps, graph = build()
    dispatch(client)
    alert = first_alert(deps)
    graph.requests.clear()
    response = post(client, inbound(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": f"SAFE:{alert.id}", "title": "Estoy a salvo"}}))
    assert response.status_code == 200
    assert alert.response is ResponseKind.SAFE and alert.response_channel == "whatsapp"
    reply = graph.message_bodies()[0]
    assert reply["to"] == WA and reply["text"]["body"] == "Gracias, registramos que está a salvo."
    assert deps.messaging.mirror.messages_for("+51900000001")[-1].text == "Gracias, registramos que está a salvo."


def test_a_help_button_opens_a_case_visible_in_the_console():
    client, deps, _ = build()
    dispatch(client)
    alert = first_alert(deps)
    post(client, inbound(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": f"HELP:{alert.id}", "title": "Necesito ayuda"}}))
    cases = client.get("/alerts/cases", params={"status": "open"}).json()["cases"]
    assert any(c["alert_id"] == alert.id and c["reason"] == "NEEDS_HELP" for c in cases)


def test_a_duplicate_delivery_is_processed_once():
    client, deps, graph = build()
    dispatch(client)
    alert = first_alert(deps)
    graph.requests.clear()
    payload = inbound(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": f"HELP:{alert.id}", "title": "x"}})
    assert post(client, payload).status_code == 200 and post(client, payload).status_code == 200
    assert len(graph.message_bodies()) == 1 and len(deps.ledger.list_cases()) == 1


def test_a_text_command_answers_the_most_recent_alert_of_that_phone():
    client, deps, _ = build()
    dispatch(client)
    alert = first_alert(deps)
    post(client, inbound(type="text", text={"body": "2"}))
    assert alert.response is ResponseKind.NEEDS_HELP


def test_free_text_is_answered_by_the_model_through_the_assistant():
    model = FakeModel()
    client, _, graph = build(agent_model=model)
    post(client, inbound(type="text", text={"body": "¿Qué hago si llueve fuerte?"}))
    assert model.calls and model.calls[0]["tools"] == []
    body = graph.message_bodies()[0]["text"]["body"]
    assert "Revise su punto seguro." in body and "SENAMHI" in body and len(body) <= 600


def test_free_text_without_the_model_gets_the_menu():
    client, _, graph = build()
    post(client, inbound(type="text", text={"body": "¿Qué hago?"}))
    assert "Responda 1" in graph.message_bodies()[0]["text"]["body"]


def test_a_location_is_attached_to_the_open_case():
    client, deps, _ = build()
    dispatch(client)
    alert = first_alert(deps)
    post(client, inbound(type="interactive", id="w1", interactive={"type": "button_reply", "button_reply": {"id": f"HELP:{alert.id}", "title": "x"}}))
    payload = inbound(type="location", location={"latitude": -11.9, "longitude": -76.5, "name": "Puente"})
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.IN2"
    post(client, payload)
    case = next(c for c in client.get("/alerts/cases").json()["cases"] if c["alert_id"] == alert.id)
    assert case["reported_lat"] == -11.9 and case["reported_lon"] == -76.5 and "Puente" in case["location_note"]


def test_a_voice_note_is_transcribed_and_handled_like_text():
    graph = FakeGraph(media={"7001": b"OggS"})
    client, deps, _ = build(graph=graph, audio_converter=FakeConverter(), transcriber=FakeTranscriber(), agent_model=FakeModel())
    dispatch(client)
    alert = first_alert(deps)
    post(client, inbound(type="audio", audio={"id": "7001", "mime_type": "audio/ogg; codecs=opus", "voice": True}))
    assert alert.response is ResponseKind.SAFE


def test_a_voice_note_without_a_decoder_asks_for_text():
    graph = FakeGraph(media={"7001": b"OggS"})
    client, _, graph = build(graph=graph, audio_converter=None, transcriber=FakeTranscriber())
    post(client, inbound(type="audio", audio={"id": "7001", "mime_type": "audio/ogg"}))
    assert graph.message_bodies()[0]["text"]["body"] == "Por ahora no puedo escuchar audios, escríbame por favor."


def test_a_video_opens_a_watcher_report_case_and_stores_the_clip():
    graph = FakeGraph(media={"7002": b"mp4-bytes"})
    client, deps, _ = build(graph=graph)
    post(client, inbound(type="video", video={"id": "7002", "mime_type": "video/mp4"}))
    (case,) = deps.ledger.list_cases()
    assert case.kind == "watcher_report" and deps.clips.ids()
    body = client.get("/alerts/cases").json()["cases"][0]
    assert body["kind"] == "watcher_report"


def test_a_sender_outside_the_allowlist_is_ignored():
    model = FakeModel()
    client, _, graph = build(agent_model=model, env={**ENV, "WILLAY_CALL_ALLOWLIST": "+51900000002"})
    payload = inbound(type="text", text={"body": "hola"})
    assert post(client, payload).status_code == 200
    assert graph.requests == [] and model.calls == []


def test_a_delivery_status_updates_the_alert_message_status():
    client, deps, _ = build()
    dispatch(client)
    alert = first_alert(deps)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {"statuses": [{"id": alert.message_id, "status": "delivered", "recipient_id": WA}]}}]}],
    }
    assert post(client, payload).status_code == 200
    assert alert.message_status == "delivered"
    household = next(a for a in client.get("/alerts/households").json()["alerts"] if a["id"] == alert.id)
    assert household["message_status"] == "delivered"


def test_a_valid_payload_with_nothing_to_do_still_answers_200():
    client, _, _ = build()
    assert post(client, {"object": "whatsapp_business_account", "entry": []}).status_code == 200
    assert post(client, {"object": "page"}).status_code == 200


# --- operator test endpoint ----------------------------------------------------------------------------------------


def test_the_test_endpoint_sends_a_spanish_hello_with_two_buttons_to_an_allowlisted_number():
    client, deps, graph = build()
    response = client.post("/whatsapp/test", json={"to": "+51900000001"})
    assert response.status_code == 200
    body = response.json()
    assert body["sent"] is True and body["used_template"] is False and "900000001" not in json.dumps(body)
    sent = graph.message_bodies()[0]
    titles = [b["reply"]["title"] for b in sent["interactive"]["action"]["buttons"]]
    assert titles == ["Estoy a salvo", "Necesito ayuda"] and "Willay" in sent["interactive"]["body"]["text"]
    assert deps.messaging.mirror.messages_for("+51900000001")


def test_the_test_endpoint_refuses_numbers_outside_the_allowlist_and_junk():
    client, _, graph = build()
    assert client.post("/whatsapp/test", json={"to": "+51999999999"}).status_code == 403
    assert client.post("/whatsapp/test", json={"to": "hola"}).status_code in (403, 422)
    assert client.post("/whatsapp/test", json={}).status_code == 422
    assert graph.requests == []


def test_the_test_endpoint_surfaces_a_meta_error_without_the_token():
    client, _, _ = build(graph=FakeGraph(fail_codes=[131030]))
    response = client.post("/whatsapp/test", json={"to": "+51900000001"})
    assert response.status_code == 502 and "131030" in response.text and TOKEN not in response.text


def test_the_test_endpoint_reports_the_template_fallback_outside_the_24h_window():
    client, _, _ = build(graph=FakeGraph(fail_codes=[131047]))
    body = client.post("/whatsapp/test", json={"to": "+51900000001"}).json()
    assert body["sent"] is True and body["used_template"] is True and "24" in body["note"]


# --- log -------------------------------------------------------------------------------------------------------------


def test_the_log_lists_masked_inbound_and_outbound_events_newest_first():
    client, _, _ = build()
    client.post("/whatsapp/test", json={"to": "+51900000001"})
    post(client, inbound(type="text", text={"body": "hola"}))
    body = client.get("/whatsapp/log").json()
    events = body["events"]
    directions = {e["direction"] for e in events}
    assert {"in", "out"} <= directions
    text = json.dumps(body)
    assert "51900000001" not in text and TOKEN not in text and SECRET not in text
    assert body["messaging_mode"].startswith("whatsapp")

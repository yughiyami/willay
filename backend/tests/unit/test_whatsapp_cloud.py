"""WhatsApp Cloud API adapter, fully mocked: no request leaves the machine and no message is sent."""

import hashlib
import hmac
import json

import httpx
import pytest

from willay.adapters.whatsapp_cloud import (
    DEFAULT_GRAPH_VERSION,
    MissingWhatsAppConfigError,
    WhatsAppCloudMessenger,
    WhatsAppConfig,
    WhatsAppEventLog,
    parse_webhook_payload,
)
from willay.ports.messaging import MessageError, MessageNotAllowed

TOKEN, PHONE_ID, VERIFY, SECRET = "EAAG-token-not-real", "106540352242922", "verify-me", "app-secret-not-real"
ALLOWED = "+51900000001"
ENV = {
    "WHATSAPP_TOKEN": TOKEN,
    "WHATSAPP_PHONE_NUMBER_ID": PHONE_ID,
    "WHATSAPP_VERIFY_TOKEN": VERIFY,
    "WHATSAPP_APP_SECRET": SECRET,
    "WILLAY_CALL_ALLOWLIST": ALLOWED,
}


class FakeGraph:
    """Records requests; `script` maps a predicate on the JSON body to a response."""

    def __init__(self, fail_codes=(), media=None):
        self.requests: list[httpx.Request] = []
        self.fail_codes = list(fail_codes)  # error codes returned by the next message POSTs
        self.media = media or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/messages"):
            if self.fail_codes:
                code = self.fail_codes.pop(0)
                return httpx.Response(400, json={"error": {"message": f"boom {TOKEN}", "type": "OAuthException", "code": code}})
            return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": f"wamid.{len(self.requests)}"}]})
        if request.method == "GET" and request.url.host == "graph.facebook.com":
            media_id = request.url.path.rsplit("/", 1)[-1]
            if media_id in self.media:
                return httpx.Response(
                    200, json={"id": media_id, "url": f"https://lookaside.fbsbx.com/{media_id}", "mime_type": "audio/ogg; codecs=opus", "file_size": len(self.media[media_id])}
                )
            return httpx.Response(404, json={"error": {"message": "nope", "code": 100}})
        if request.method == "GET" and request.url.host == "lookaside.fbsbx.com":
            return httpx.Response(200, content=self.media[request.url.path.lstrip("/")], headers={"content-type": "audio/ogg"})
        return httpx.Response(500)

    def message_bodies(self):
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


def make(fake=None, env=None, log=None):
    fake = fake or FakeGraph()
    config = WhatsAppConfig.from_env({**ENV, **(env or {})})
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return WhatsAppCloudMessenger(config, client=client, log=log), fake


# --- configuration ------------------------------------------------------------------------------------


def test_config_names_every_missing_variable_and_never_a_value():
    with pytest.raises(MissingWhatsAppConfigError) as exc:
        WhatsAppConfig.from_env({"WHATSAPP_TOKEN": TOKEN})
    text = str(exc.value)
    assert "WHATSAPP_PHONE_NUMBER_ID" in text and "WHATSAPP_VERIFY_TOKEN" in text and "WHATSAPP_APP_SECRET" in text
    assert TOKEN not in text


def test_config_defaults_the_graph_version_and_normalizes_a_bare_number():
    assert WhatsAppConfig.from_env(ENV).graph_version == DEFAULT_GRAPH_VERSION == "v26.0"
    assert WhatsAppConfig.from_env({**ENV, "WHATSAPP_GRAPH_VERSION": "25.0"}).graph_version == "v25.0"
    with pytest.raises(ValueError):
        WhatsAppConfig.from_env({**ENV, "WHATSAPP_GRAPH_VERSION": "latest"})
    with pytest.raises(ValueError):
        WhatsAppConfig.from_env({**ENV, "WHATSAPP_PHONE_NUMBER_ID": "abc/../x"})


def test_message_allowlist_overrides_the_call_allowlist():
    config = WhatsAppConfig.from_env({**ENV, "WILLAY_MESSAGE_ALLOWLIST": "+51911111111"})
    assert config.allowlist == frozenset({"+51911111111"})
    assert WhatsAppConfig.from_env(ENV).allowlist == frozenset({ALLOWED})


def test_secrets_never_appear_in_repr():
    messenger, _ = make()
    text = repr(messenger) + repr(WhatsAppConfig.from_env(ENV))
    for secret in (TOKEN, VERIFY, SECRET):
        assert secret not in text


# --- sending ----------------------------------------------------------------------------------------------


def test_send_posts_a_text_message_without_the_plus_and_with_a_bearer_token():
    messenger, fake = make()
    messenger.send(ALLOWED, "Hola")
    request = fake.requests[0]
    assert str(request.url) == f"https://graph.facebook.com/v26.0/{PHONE_ID}/messages"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = fake.message_bodies()[0]
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "51900000001",
        "type": "text",
        "text": {"preview_url": False, "body": "Hola"},
    }


def test_send_text_returns_the_message_id():
    messenger, _ = make()
    result = messenger.send_text("51900000001", "Hola")  # a bare wa_id is accepted too
    assert result.message_id.startswith("wamid.") and result.used_template is False


def test_numbers_outside_the_allowlist_are_refused_before_any_request():
    messenger, fake = make()
    for number in ("+51999999999", "", "abc"):
        with pytest.raises(MessageNotAllowed):
            messenger.send(number, "Hola")
    with pytest.raises(MessageNotAllowed):
        messenger.send_buttons("+51999999999", "Hola", [("a", "Si")])
    with pytest.raises(MessageNotAllowed):
        messenger.send_template("+51999999999", "hello_world")
    assert fake.requests == []


def test_an_empty_allowlist_refuses_everything():
    messenger, fake = make(env={"WILLAY_CALL_ALLOWLIST": ""})
    with pytest.raises(MessageNotAllowed):
        messenger.send(ALLOWED, "Hola")
    assert fake.requests == []


def test_send_buttons_builds_an_interactive_reply_button_message():
    messenger, fake = make()
    messenger.send_buttons(ALLOWED, "Alerta", [("SAFE:alert-0001", "Estoy a salvo"), ("HELP:alert-0001", "Necesito ayuda")])
    body = fake.message_bodies()[0]
    assert body["type"] == "interactive" and body["to"] == "51900000001"
    interactive = body["interactive"]
    assert interactive["type"] == "button" and interactive["body"] == {"text": "Alerta"}
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "SAFE:alert-0001", "title": "Estoy a salvo"}},
        {"type": "reply", "reply": {"id": "HELP:alert-0001", "title": "Necesito ayuda"}},
    ]


@pytest.mark.parametrize(
    "buttons",
    [
        [],
        [("1", "a"), ("2", "b"), ("3", "c"), ("4", "d")],
        [("x" * 257, "Si")],
        [("a", "t" * 21)],
        [("a", "Igual"), ("b", "Igual")],
        [("a", "Si"), ("a", "No")],
        [("", "Si")],
    ],
)
def test_send_buttons_enforces_the_cloud_api_limits(buttons):
    messenger, fake = make()
    with pytest.raises(ValueError):
        messenger.send_buttons(ALLOWED, "Hola", buttons)
    assert fake.requests == []


def test_send_buttons_truncates_a_body_over_1024_characters():
    messenger, fake = make()
    messenger.send_buttons(ALLOWED, "x" * 2000, [("a", "Si")])
    assert len(fake.message_bodies()[0]["interactive"]["body"]["text"]) == 1024


def test_send_template_posts_a_template_message():
    messenger, fake = make()
    messenger.send_template(ALLOWED, "hello_world", "en_US")
    body = fake.message_bodies()[0]
    assert body["type"] == "template"
    assert body["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


def test_outside_the_24h_window_falls_back_to_the_hello_world_template_and_says_so():
    log = WhatsAppEventLog()
    messenger, fake = make(FakeGraph(fail_codes=[131047]), log=log)
    result = messenger.send_text(ALLOWED, "Alerta importante")
    assert result.used_template is True and result.message_id
    types = [b["type"] for b in fake.message_bodies()]
    assert types == ["text", "template"]
    assert fake.message_bodies()[1]["template"]["name"] == "hello_world"
    assert any("131047" in e["detail"] and "template" in e["detail"] for e in log.recent())


def test_the_template_fallback_also_covers_buttons():
    messenger, fake = make(FakeGraph(fail_codes=[131047]))
    result = messenger.send_buttons(ALLOWED, "Alerta", [("a", "Si")])
    assert result.used_template is True
    assert [b["type"] for b in fake.message_bodies()] == ["interactive", "template"]


def test_a_failed_template_fallback_raises():
    messenger, _ = make(FakeGraph(fail_codes=[131047, 131047]))
    with pytest.raises(MessageError):
        messenger.send_text(ALLOWED, "Alerta")


def test_recipient_not_in_the_test_allowlist_131030_is_surfaced_and_raised():
    log = WhatsAppEventLog()
    messenger, _ = make(FakeGraph(fail_codes=[131030]), log=log)
    with pytest.raises(MessageError) as exc:
        messenger.send(ALLOWED, "Hola")
    assert exc.value.code == 131030
    assert any("131030" in e["detail"] and "test" in e["detail"].lower() for e in log.recent())


def test_errors_never_leak_the_token():
    messenger, _ = make(FakeGraph(fail_codes=[190]))
    with pytest.raises(MessageError) as exc:
        messenger.send(ALLOWED, "Hola")
    assert TOKEN not in str(exc.value)


def test_a_network_failure_becomes_a_message_error_without_details():
    def boom(request):
        raise httpx.ConnectError(f"cannot connect with {TOKEN}")

    config = WhatsAppConfig.from_env(ENV)
    messenger = WhatsAppCloudMessenger(config, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(MessageError) as exc:
        messenger.send(ALLOWED, "Hola")
    assert TOKEN not in str(exc.value) and "ConnectError" in str(exc.value)


# --- media -------------------------------------------------------------------------------------------------


def test_download_media_resolves_the_url_then_downloads_with_the_same_bearer_token():
    messenger, fake = make(FakeGraph(media={"555": b"OggS-bytes"}))
    data, mime = messenger.download_media("555")
    assert data == b"OggS-bytes" and mime == "audio/ogg"
    assert [r.url.host for r in fake.requests] == ["graph.facebook.com", "lookaside.fbsbx.com"]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)


def test_download_media_rejects_a_malformed_id_and_oversize_files():
    messenger, fake = make(FakeGraph(media={"555": b"x" * 100}))
    with pytest.raises(MessageError):
        messenger.download_media("../me")
    with pytest.raises(MessageError):
        messenger.download_media("555", max_bytes=10)
    with pytest.raises(MessageError):
        messenger.download_media("999")  # unknown id: Graph answers 404


# --- webhook security ---------------------------------------------------------------------------------------


def test_signature_is_an_hmac_sha256_of_the_raw_body():
    messenger, _ = make()
    raw = b'{"object":"whatsapp_business_account"}'
    good = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert messenger.validate_signature(raw, good) is True
    assert messenger.validate_signature(raw + b" ", good) is False
    assert messenger.validate_signature(raw, None) is False
    assert messenger.validate_signature(raw, "sha256=deadbeef") is False
    assert messenger.validate_signature(raw, good.removeprefix("sha256=")) is False


def test_verify_token_comparison():
    messenger, _ = make()
    assert messenger.verify_token_matches(VERIFY) is True
    assert messenger.verify_token_matches("nope") is False
    assert messenger.verify_token_matches(None) is False


# --- webhook parsing ----------------------------------------------------------------------------------------


def envelope(**value):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "1", "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp", **value}}]}],
    }


def parse_one(message):
    messages, _ = parse_webhook_payload(envelope(messages=[{"from": "51900000001", "id": "wamid.A", "timestamp": "1", **message}]))
    assert len(messages) == 1
    return messages[0]


def test_parses_a_text_message():
    m = parse_one({"type": "text", "text": {"body": "hola"}})
    assert (m.id, m.sender, m.type, m.text) == ("wamid.A", "51900000001", "text", "hola")


def test_parses_a_button_reply():
    m = parse_one({"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "SAFE:alert-0001", "title": "Estoy a salvo"}}})
    assert m.type == "button_reply" and m.button_id == "SAFE:alert-0001"


def test_parses_audio_video_image_and_location():
    audio = parse_one({"type": "audio", "audio": {"id": "A1", "mime_type": "audio/ogg; codecs=opus", "voice": True}})
    assert audio.type == "audio" and audio.media_id == "A1" and audio.mime_type.startswith("audio/ogg")
    video = parse_one({"type": "video", "video": {"id": "V1", "mime_type": "video/mp4", "caption": "rio"}})
    assert video.type == "video" and video.media_id == "V1" and video.caption == "rio"
    image = parse_one({"type": "image", "image": {"id": "I1", "mime_type": "image/jpeg"}})
    assert image.type == "image" and image.media_id == "I1"
    where = parse_one({"type": "location", "location": {"latitude": -11.9, "longitude": -76.5, "name": "Puente", "address": "Km 5"}})
    assert (where.type, where.latitude, where.longitude, where.location_name) == ("location", -11.9, -76.5, "Puente Km 5")


def test_parses_statuses_including_failures():
    payload = envelope(
        statuses=[
            {"id": "wamid.S", "status": "delivered", "recipient_id": "51900000001", "timestamp": "1"},
            {"id": "wamid.F", "status": "failed", "recipient_id": "51900000001", "errors": [{"code": 131047, "title": "Re-engagement message"}]},
        ]
    )
    _, statuses = parse_webhook_payload(payload)
    assert [(s.message_id, s.status, s.error_code) for s in statuses] == [("wamid.S", "delivered", None), ("wamid.F", "failed", 131047)]


def test_walks_every_entry_and_change_and_ignores_other_fields():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {"changes": [{"field": "messages", "value": {"messages": [{"from": "1", "id": "w1", "type": "text", "text": {"body": "a"}}]}}]},
            {
                "changes": [
                    {"field": "account_update", "value": {"messages": [{"from": "2", "id": "w2", "type": "text", "text": {"body": "x"}}]}},
                    {"field": "messages", "value": {"messages": [{"from": "3", "id": "w3", "type": "text", "text": {"body": "c"}}]}},
                ]
            },
        ],
    }
    messages, _ = parse_webhook_payload(payload)
    assert [m.id for m in messages] == ["w1", "w3"]


def test_a_message_without_a_phone_is_tolerated_and_keyed_by_the_user_id():
    payload = envelope(messages=[{"id": "w1", "type": "text", "text": {"body": "hola"}, "from_user_id": "BSUID.123"}])
    messages, _ = parse_webhook_payload(payload)
    assert len(messages) == 1 and messages[0].sender == "" and messages[0].user_id == "BSUID.123"


@pytest.mark.parametrize("payload", [None, [], {}, {"entry": "x"}, {"entry": [None]}, {"entry": [{"changes": [{"field": "messages", "value": {"messages": [None, 3, {"type": "text"}]}}]}]}])
def test_malformed_payloads_never_raise(payload):
    messages, statuses = parse_webhook_payload(payload)
    assert statuses == [] and all(m.id for m in messages)


# --- event log ---------------------------------------------------------------------------------------------


def test_event_log_masks_phones_and_is_bounded():
    log = WhatsAppEventLog(max_events=3)
    for i in range(5):
        log.record("in", "text", "51900000001", f"event {i}")
    events = log.recent()
    assert len(events) == 3 and events[0]["detail"] == "event 4"  # newest first
    assert "51900000001" not in json.dumps(events) and events[0]["phone"].startswith("519")

"""Telegram bot over a fake Bot API (httpx.MockTransport): subscription, alerts with buttons, callbacks, location, voice."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from willay.adapters.telegram_bot import MAX_CHATS, TelegramApi, TelegramBot, TelegramConfig, TelegramError
from willay.api.app import create_app
from willay.api.dependencies import attach_telegram_from_env, synthetic_dependencies

TOKEN = "123456:SECRET-token-value"
CHAT = 555001


class FakeBotApi:
    """Records every call and answers like the Bot API."""

    def __init__(self, fail_methods=()):
        self.calls: list[tuple[str, dict]] = []
        self.fail_methods = set(fail_methods)
        self.updates: list[list[dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/"):
            return httpx.Response(200, content=b"OggS-voice")
        method = path.rsplit("/", 1)[-1]
        content_type = request.headers.get("content-type", "")
        payload = json.loads(request.content) if content_type.startswith("application/json") and request.content else {}
        self.calls.append((method, payload))
        if method in self.fail_methods:
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": f"bad request for {TOKEN}"})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/a.oga", "file_size": 10}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates.pop(0) if self.updates else []})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method="sendMessage") -> list[dict]:
        return [p for m, p in self.calls if m == method]


class FakeAssistant:
    def answer(self, text):
        return "Revise su punto seguro. Willay complementa a SENAMHI e INDECI."


class FakeConverter:
    def to_wav(self, audio, mime):
        return b"wav"


class FakeTranscriber:
    def __init__(self, text="estoy a salvo"):
        self.text = text

    def transcribe(self, audio, language="es"):
        return self.text


def build(allowed=(), fail_methods=(), transcript="estoy a salvo", **deps_fields):
    fake = FakeBotApi(fail_methods)
    deps = synthetic_dependencies()
    deps.telegram_config = TelegramConfig(TOKEN, frozenset(allowed))
    deps.telegram_http_client = httpx.Client(transport=httpx.MockTransport(fake))
    deps.audio_converter, deps.transcriber = FakeConverter(), FakeTranscriber(transcript)
    for name, value in deps_fields.items():
        setattr(deps, name, value)
    client = TestClient(create_app(deps))
    return client, deps, deps.telegram, fake


def message(text="", chat=CHAT, **extra):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"first_name": "Ana"}, "text": text, **extra}}


def dispatch(client, tier="ATTENTION", **extra):
    return client.post("/alerts/dispatch", json={"site_id": "syn-001", "tier": tier, **extra}).json()


def test_disabled_without_a_token_and_the_token_is_never_in_config_repr():
    deps = attach_telegram_from_env(synthetic_dependencies(), environ={})
    assert deps.telegram_config is None and deps.telegram_mode.startswith("disabled")
    config = TelegramConfig.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_CHAT_IDS": "12, -100,x"})
    assert config is not None and config.allowed_chat_ids == frozenset({12, -100})
    assert "SECRET" not in repr(config)
    enabled = attach_telegram_from_env(synthetic_dependencies(), environ={"TELEGRAM_BOT_TOKEN": TOKEN})
    assert enabled.telegram_mode == "live (0 chats)" and "SECRET" not in enabled.telegram_mode


def test_status_and_health_before_and_after_start():
    client, _, bot, _ = build()
    assert client.get("/telegram/status").json()["enabled"] is True
    assert client.get("/health").json()["telegram_mode"] == "live (0 chats)"
    bot.handle_update(message("/start"))
    status = client.get("/telegram/status").json()
    assert status["chats"] == 1 and status["last_error"] is None and str(CHAT) not in json.dumps(status)
    assert client.get("/health").json()["telegram_mode"] == "live (1 chats)"


def test_start_subscribes_and_sends_the_welcome_other_chats_must_send_start():
    _, _, bot, fake = build()
    assert bot.handle_update(message("hola")) == "not_subscribed"
    assert "/start" in fake.sent()[-1]["text"]
    assert bot.handle_update(message("/start")) == "welcome"
    assert "SENAMHI" in fake.sent()[-1]["text"] and fake.sent()[-1]["chat_id"] == CHAT
    assert bot.handle_update(message("/start@WillayBot")) == "welcome"
    assert bot.chat_count == 1


def test_open_mode_caps_the_number_of_chats():
    _, _, bot, _ = build()
    for chat in range(MAX_CHATS + 3):
        bot.handle_update(message("/start", chat=1000 + chat))
    assert bot.chat_count == MAX_CHATS


def test_allow_list_serves_only_listed_chats_without_start():
    _, _, bot, fake = build(allowed={CHAT})
    assert bot.handle_update(message("hola", chat=999)) == "not_subscribed" and fake.sent() == []
    assert bot.handle_update(message("hola")) == "text"
    assert bot.chat_count == 1 and "SENAMHI" in fake.sent()[-1]["text"]


def test_dispatch_sends_the_alert_with_inline_buttons_and_the_callback_updates_the_console():
    client, _, bot, fake = build()
    bot.handle_update(message("/start"))
    dispatch(client)
    alert = fake.sent()[-1]
    keyboard = alert["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in keyboard] == ["Estoy a salvo", "Necesito ayuda"]
    assert keyboard[0]["callback_data"].startswith("SAFE:alert-") and len(keyboard[0]["callback_data"]) <= 64

    help_data = keyboard[1]["callback_data"]
    callback = {"update_id": 2, "callback_query": {"id": "cb1", "data": help_data, "from": {"id": CHAT}, "message": {"chat": {"id": CHAT}}}}
    assert bot.handle_update(callback) == "button"
    assert fake.sent("answerCallbackQuery")[-1]["callback_query_id"] == "cb1"
    assert "operador" in fake.sent()[-1]["text"]
    alert_id = help_data.split(":")[1]
    alerts = {a["id"]: a for a in client.get("/alerts/households").json()["alerts"]}
    assert alerts[alert_id]["response"] == "NEEDS_HELP" and alerts[alert_id]["response_channel"] == "telegram"
    assert any(c["alert_id"] == alert_id and c["status"] == "open" for c in client.get("/alerts/cases").json()["cases"])


def test_a_callback_from_an_unsubscribed_chat_is_ignored_safely():
    _, _, bot, fake = build()
    callback = {"update_id": 2, "callback_query": {"id": "cb1", "data": "SAFE:alert-0001", "message": {"chat": {"id": 42}}}}
    assert bot.handle_update(callback) == "not_subscribed"
    assert fake.sent("answerCallbackQuery")


def test_location_goes_to_the_open_case_of_the_bound_household():
    client, _, bot, fake = build()
    bot.handle_update(message("/start"))
    dispatch(client)
    help_data = fake.sent()[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    bot.handle_update({"update_id": 3, "callback_query": {"id": "c", "data": help_data, "message": {"chat": {"id": CHAT}}}})
    assert bot.handle_update(message(location={"latitude": -11.95, "longitude": -76.69})) == "location"
    case = next(c for c in client.get("/alerts/cases").json()["cases"] if c["alert_id"] == help_data.split(":")[1] and c["status"] == "open")
    assert (case["reported_lat"], case["reported_lon"]) == (-11.95, -76.69) and "Telegram" in case["location_note"]


def test_voice_note_is_downloaded_transcribed_and_answered():
    client, _, bot, fake = build(transcript="estoy a salvo")
    bot.handle_update(message("/start"))
    dispatch(client)
    assert bot.handle_update(message(voice={"file_id": "f1", "mime_type": "audio/ogg"})) == "audio"
    assert any(m == "getFile" for m, _ in fake.calls)
    assert "a salvo" in fake.sent()[-1]["text"]
    alerts = client.get("/alerts/households").json()["alerts"]
    assert any(a["response_channel"] == "telegram" and a["response"] == "SAFE" for a in alerts)


def test_voice_note_failure_is_polite_and_text_uses_the_guarded_assistant():
    _, _, bot, fake = build(fail_methods={"getFile"}, agent_model=None)
    bot.handle_update(message("/start"))
    assert bot.handle_update(message(voice={"file_id": "f1"})) == "audio_failed"
    assert "No pude procesar" in fake.sent()[-1]["text"]
    assert TOKEN not in (bot.last_error or "") and bot.last_error


def test_one_failing_chat_does_not_stop_the_others_and_errors_hide_the_token():
    client, deps, bot, fake = build(fail_methods={"sendMessage"})
    bot.handle_update(message("/start"))  # its welcome fails to send, but the chat is subscribed
    client.post("/webchat/sessions")
    body = dispatch(client)
    assert body["households_targeted"] >= 3  # the dispatch itself is not affected
    web = deps.webchat.session_ids()[0]
    assert deps.webchat.messages_after(web)[-1]["kind"] == "buttons"  # the web chat still got the alert


def test_api_errors_never_contain_the_token():
    def handler(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    api = TelegramApi(TOKEN, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(TelegramError) as caught:
        api.send_message(1, "hi")
    assert TOKEN not in str(caught.value) and "ConnectError" in str(caught.value)
    failing = TelegramApi(TOKEN, httpx.Client(transport=httpx.MockTransport(FakeBotApi({"sendMessage"}))))
    with pytest.raises(TelegramError) as described:
        failing.send_message(1, "hi")
    assert TOKEN not in str(described.value)


def test_poll_loop_processes_updates_advances_the_offset_and_records_errors():
    fake = FakeBotApi()
    fake.updates = [[message("/start") | {"update_id": 10}]]
    deps = synthetic_dependencies()
    api = TelegramApi(TOKEN, httpx.Client(transport=httpx.MockTransport(fake)))
    from willay.application.citizen_chat import CitizenChat

    bot = TelegramBot(TelegramConfig(TOKEN), api, CitizenChat(ledger=deps.ledger, source="telegram"), sleep=lambda s: None)
    polls = {"n": 0}
    original = api.get_updates

    def limited(offset, timeout=25):
        polls["n"] += 1
        if polls["n"] > 2:
            bot.stop()
            return []
        return original(offset, timeout)

    api.get_updates = limited  # type: ignore[method-assign]
    bot._run()
    assert bot.chat_count == 1 and bot._offset == 11
    assert [m for m, _ in fake.calls][0] == "deleteWebhook"

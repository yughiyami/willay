"""WILLAY_MESSAGING_PROVIDER wiring and the composite that keeps the web phone mock-up working."""

import pytest

from willay.adapters.audio_convert import build_audio_converter
from willay.adapters.composite_messaging import MirroredMessaging
from willay.adapters.web_whatsapp import WebWhatsAppSimulator
from willay.adapters.whatsapp_cloud import WhatsAppCloudMessenger
from willay.api.dependencies import attach_messaging_from_env, synthetic_dependencies
from willay.ports.messaging import InteractiveMessagingPort, MessageError, SendResult

ENV = {
    "WILLAY_MESSAGING_PROVIDER": "whatsapp",
    "WHATSAPP_TOKEN": "tok-secret-value",
    "WHATSAPP_PHONE_NUMBER_ID": "123456",
    "WHATSAPP_VERIFY_TOKEN": "verify-secret-value",
    "WHATSAPP_APP_SECRET": "app-secret-value",
    "WILLAY_CALL_ALLOWLIST": "+51900000001",
}


def test_the_simulator_is_the_default():
    deps = attach_messaging_from_env(synthetic_dependencies(), environ={})
    assert isinstance(deps.messaging, WebWhatsAppSimulator) and deps.whatsapp is None
    assert deps.messaging_mode == "simulated"


def test_an_explicit_simulated_provider_changes_nothing():
    deps = attach_messaging_from_env(synthetic_dependencies(), environ={"WILLAY_MESSAGING_PROVIDER": "simulated"})
    assert isinstance(deps.messaging, WebWhatsAppSimulator) and deps.messaging_mode == "simulated"


def test_incomplete_whatsapp_config_keeps_the_simulator_and_names_what_is_missing():
    env = {"WILLAY_MESSAGING_PROVIDER": "whatsapp", "WHATSAPP_TOKEN": "tok-secret-value"}
    deps = attach_messaging_from_env(synthetic_dependencies(), environ=env)
    assert isinstance(deps.messaging, WebWhatsAppSimulator) and deps.whatsapp is None
    assert deps.messaging_mode.startswith("simulated") and "WHATSAPP_APP_SECRET" in deps.messaging_mode
    assert "tok-secret-value" not in deps.messaging_mode


def test_an_invalid_config_keeps_the_simulator():
    deps = attach_messaging_from_env(synthetic_dependencies(), environ={**ENV, "WHATSAPP_GRAPH_VERSION": "latest"})
    assert isinstance(deps.messaging, WebWhatsAppSimulator) and "invalid" in deps.messaging_mode


def test_an_unknown_provider_keeps_the_simulator():
    deps = attach_messaging_from_env(synthetic_dependencies(), environ={"WILLAY_MESSAGING_PROVIDER": "telegram"})
    assert isinstance(deps.messaging, WebWhatsAppSimulator) and "unknown" in deps.messaging_mode


def test_a_complete_config_wires_whatsapp_mirrored_into_the_simulator():
    base = synthetic_dependencies()
    simulator = base.messaging
    deps = attach_messaging_from_env(base, environ=ENV)
    assert isinstance(deps.whatsapp, WhatsAppCloudMessenger)
    assert isinstance(deps.messaging, MirroredMessaging) and deps.messaging.mirror is simulator
    assert deps.messaging_mode.startswith("whatsapp") and "1 number" in deps.messaging_mode
    for secret in ("tok-secret-value", "verify-secret-value", "app-secret-value"):
        assert secret not in deps.messaging_mode


# --- composite -----------------------------------------------------------------------------------------------


class Primary:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, recipient, text):
        if self.fail:
            raise MessageError("refused")
        self.sent.append((recipient, text))

    def send_buttons(self, recipient, body, buttons):
        if self.fail:
            raise MessageError("refused")
        self.sent.append((recipient, body, list(buttons)))
        return SendResult("wamid.1")


def test_the_composite_sends_to_whatsapp_and_records_in_the_simulator():
    primary, simulator = Primary(), WebWhatsAppSimulator()
    messaging = MirroredMessaging(primary, simulator)
    messaging.send("+51900000001", "Hola")
    assert primary.sent == [("+51900000001", "Hola")]
    assert [m.text for m in simulator.messages_for("+51900000001")] == ["Hola"]


def test_the_composite_exposes_buttons_and_mirrors_them_as_text():
    primary, simulator = Primary(), WebWhatsAppSimulator()
    messaging = MirroredMessaging(primary, simulator)
    assert isinstance(messaging, InteractiveMessagingPort)
    result = messaging.send_buttons("+51900000001", "Alerta", [("SAFE:1", "Estoy a salvo"), ("HELP:1", "Necesito ayuda")])
    assert result.message_id == "wamid.1"
    (mirrored,) = simulator.messages_for("+51900000001")
    assert "Alerta" in mirrored.text and "Estoy a salvo" in mirrored.text and "Necesito ayuda" in mirrored.text


def test_a_refused_send_is_not_mirrored_as_delivered_and_the_error_propagates():
    primary, simulator = Primary(fail=True), WebWhatsAppSimulator()
    messaging = MirroredMessaging(primary, simulator)
    with pytest.raises(MessageError):
        messaging.send("+51900000001", "Hola")
    assert simulator.all_messages() == []


def test_the_composite_reset_clears_the_simulator():
    simulator = WebWhatsAppSimulator()
    messaging = MirroredMessaging(Primary(), simulator)
    messaging.send("+51900000001", "Hola")
    messaging.reset()
    assert simulator.all_messages() == []
    assert messaging.messages_for("+51900000001") == []


# --- audio converter selection ----------------------------------------------------------------------------------


def test_no_decoder_available_means_no_converter(monkeypatch):
    monkeypatch.setattr("willay.adapters.audio_convert.find_ffmpeg", lambda: None)
    monkeypatch.setattr("willay.adapters.audio_convert.av_available", lambda: False)
    assert build_audio_converter() is None


def test_ffmpeg_is_used_when_pyav_is_missing(monkeypatch):
    monkeypatch.setattr("willay.adapters.audio_convert.find_ffmpeg", lambda: "ffmpeg-fake")
    monkeypatch.setattr("willay.adapters.audio_convert.av_available", lambda: False)
    converter = build_audio_converter()
    assert converter is not None and type(converter).__name__ == "FfmpegAudioConverter"


def test_pyav_is_preferred_when_importable(monkeypatch):
    monkeypatch.setattr("willay.adapters.audio_convert.av_available", lambda: True)
    assert type(build_audio_converter()).__name__ == "PyAvAudioConverter"

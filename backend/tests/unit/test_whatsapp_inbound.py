"""Inbound WhatsApp handling: button replies, text commands, location, voice notes, video and delivery statuses."""

import pytest

from willay.adapters.alert_ledger import InMemoryAlertLedger
from willay.application.whatsapp_inbound import (
    AUDIO_UNAVAILABLE_ES,
    MENU_ES,
    WhatsAppInboundHandler,
)
from willay.domain.dispatch import Household, HouseholdAlert, ResponseKind
from willay.domain.entities import AlertTier
from willay.ports.messaging import DeliveryStatus, InboundMessage, MessageError

PHONE = "+51900000001"
WA = "51900000001"
OTHER_WA = "51900000002"
HOUSEHOLDS = (
    Household("hh-1", "set-1", "Hogar 1", PHONE),
    Household("hh-2", "set-1", "Hogar 2", "+51900000002"),
)


class Sent:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send(self, recipient, text):
        self.messages.append((recipient, text))


class FakeMedia:
    def __init__(self, data=b"ogg", mime="audio/ogg; codecs=opus", fail=False):
        self.data, self.mime, self.fail, self.requested = data, mime, fail, []

    def download_media(self, media_id, max_bytes=0):
        self.requested.append(media_id)
        if self.fail:
            raise MessageError("download failed")
        return self.data, self.mime


class FakeConverter:
    def __init__(self, fail=False):
        self.fail, self.seen = fail, []

    def to_wav(self, audio, mime):
        self.seen.append((audio, mime))
        if self.fail:
            raise RuntimeError("cannot decode")
        return b"RIFFwav"


class FakeTranscriber:
    def __init__(self, text="hola, hay mucha agua en el rio"):
        self.text, self.calls = text, []

    def transcribe(self, audio, language="es"):
        self.calls.append((audio, language))
        return self.text


class FakeAssistant:
    def __init__(self, answer="Respuesta del asistente."):
        self.answer_text, self.questions = answer, []

    def answer(self, text):
        self.questions.append(text)
        return self.answer_text


class Log:
    def __init__(self):
        self.events = []

    def record(self, direction, kind, phone, detail):
        self.events.append((direction, kind, phone, detail))


def alert(ledger, household=HOUSEHOLDS[0], tier=AlertTier.ATTENTION, message_id=None):
    item = HouseholdAlert(
        id=ledger.new_id("alert"), dispatch_id="dispatch-0001", site_id="syn-001", tier=tier, household=household,
        settlement_name="Hamlet One", lon=-76.5, lat=-11.9, message_id=message_id,
    )
    ledger.add_alert(item)
    return item


def msg(type="text", id="wamid.1", sender=WA, **fields):
    return InboundMessage(id=id, sender=sender, type=type, **fields)


class Setup:
    def __init__(self, **overrides):
        self.ledger = InMemoryAlertLedger()
        self.sent = Sent()
        self.log = Log()
        self.clips: list[tuple[bytes, str]] = []
        options = dict(
            ledger=self.ledger,
            messaging=self.sent,
            households=HOUSEHOLDS,
            is_allowed=lambda number: number in (PHONE, "+51900000002"),
            log=self.log,
            clip_sink=lambda data, mime: (self.clips.append((data, mime)), f"clip-{len(self.clips)}")[1],
        )
        options.update(overrides)
        self.handler = WhatsAppInboundHandler(**options)

    @property
    def replies(self):
        return [text for _, text in self.sent.messages]


# --- button replies ----------------------------------------------------------------------------------------


def test_safe_button_records_the_answer_and_confirms_in_spanish():
    s = Setup()
    a = alert(s.ledger)
    outcome = s.handler.handle_message(msg("button_reply", button_id=f"SAFE:{a.id}"))
    assert outcome == "recorded_safe"
    assert a.response is ResponseKind.SAFE and a.response_channel == "whatsapp"
    assert s.sent.messages == [(PHONE, "Gracias, registramos que está a salvo.")]


def test_help_button_opens_an_operator_case_and_asks_for_the_location():
    s = Setup()
    a = alert(s.ledger)
    outcome = s.handler.handle_message(msg("button_reply", button_id=f"HELP:{a.id}"))
    assert outcome == "recorded_help"
    assert a.response is ResponseKind.NEEDS_HELP
    assert s.ledger.open_case_for(a.id) is not None
    assert s.replies == ["Un operador fue notificado. Si puede, comparta su ubicación."]


def test_a_button_from_another_phone_is_not_recorded_and_not_answered():
    s = Setup()
    a = alert(s.ledger)
    outcome = s.handler.handle_message(msg("button_reply", sender=OTHER_WA, button_id=f"SAFE:{a.id}"))
    assert outcome == "sender_mismatch"
    assert a.response is None and s.sent.messages == []


def test_an_unknown_alert_id_gets_a_polite_reply():
    s = Setup()
    assert s.handler.handle_message(msg("button_reply", button_id="SAFE:alert-9999")) == "unknown_alert"
    assert s.sent.messages and "alerta" in s.replies[0].lower()


def test_the_test_buttons_confirm_that_the_channel_works():
    s = Setup()
    assert s.handler.handle_message(msg("button_reply", button_id="TEST:OK")) == "test_ack"
    assert "prueba" in s.replies[0].lower()


# --- text commands -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["1", " 1 ", "a salvo", "Estoy a salvo", "ESTOY A SALVO!", "Sí, estoy a salvo."])
def test_safe_commands_apply_to_the_most_recent_alert_of_that_phone(text):
    s = Setup()
    old = alert(s.ledger)
    new = alert(s.ledger)
    alert(s.ledger, HOUSEHOLDS[1])  # someone else's, even newer
    s.handler.handle_message(msg(text=text))
    assert new.response is ResponseKind.SAFE and old.response is None


@pytest.mark.parametrize("text", ["2", "ayuda", "Necesito ayuda", "AYUDA!!", "auxilio"])
def test_help_commands(text):
    s = Setup()
    a = alert(s.ledger)
    s.handler.handle_message(msg(text=text))
    assert a.response is ResponseKind.NEEDS_HELP and s.ledger.open_case_for(a.id) is not None


def test_a_negated_sentence_is_not_a_safe_command():
    s = Setup(assistant=FakeAssistant())
    a = alert(s.ledger)
    s.handler.handle_message(msg(text="no estoy a salvo, hay agua"))
    assert a.response is None


def test_a_command_without_an_alert_explains_there_is_none():
    s = Setup()
    assert s.handler.handle_message(msg(text="1")) == "no_alert"
    assert "alerta" in s.replies[0].lower()


def test_a_greeting_shows_the_menu_without_calling_the_model():
    assistant = FakeAssistant()
    s = Setup(assistant=assistant)
    assert s.handler.handle_message(msg(text="Hola")) == "menu"
    assert s.replies == [MENU_ES] and assistant.questions == []


def test_other_text_is_answered_by_the_assistant_and_capped_at_600_characters():
    assistant = FakeAssistant("x" * 900)
    s = Setup(assistant=assistant)
    assert s.handler.handle_message(msg(text="¿Qué hago si llueve fuerte?")) == "answered"
    assert assistant.questions == ["¿Qué hago si llueve fuerte?"]
    assert len(s.replies[0]) <= 600


def test_without_a_model_other_text_gets_the_menu():
    s = Setup(assistant=None)
    assert s.handler.handle_message(msg(text="¿Qué hago?")) == "menu"
    assert s.replies == [MENU_ES]


def test_a_failing_assistant_falls_back_to_the_menu():
    class Broken:
        def answer(self, text):
            raise RuntimeError("model down")

    s = Setup(assistant=Broken())
    assert s.handler.handle_message(msg(text="¿Qué hago?")) == "menu"
    assert s.replies == [MENU_ES]


def test_the_menu_mentions_the_official_sources():
    assert "SENAMHI" in MENU_ES and "INDECI" in MENU_ES and "1" in MENU_ES and "2" in MENU_ES


# --- location -----------------------------------------------------------------------------------------------


def test_a_location_is_attached_to_the_open_case_of_that_phone():
    s = Setup()
    a = alert(s.ledger)
    s.handler.handle_message(msg("button_reply", id="w1", button_id=f"HELP:{a.id}"))
    s.sent.messages.clear()
    outcome = s.handler.handle_message(msg("location", id="w2", latitude=-11.91, longitude=-76.52, location_name="Puente Viejo"))
    case = s.ledger.open_case_for(a.id)
    assert outcome == "location_attached"
    assert (case.lat, case.lon) == (-11.91, -76.52) and "Puente Viejo" in case.location_note
    assert s.replies and "ubicación" in s.replies[0].lower()


def test_a_location_without_an_open_case_is_acknowledged_honestly_and_attached_to_nothing():
    s = Setup()
    alert(s.ledger)
    assert s.handler.handle_message(msg("location", latitude=-11.9, longitude=-76.5)) == "location_no_case"
    assert s.ledger.list_cases() == []
    assert "no hay un caso abierto" in s.replies[0].lower()


def test_an_out_of_range_location_is_rejected():
    s = Setup()
    a = alert(s.ledger)
    s.handler.handle_message(msg("button_reply", id="w1", button_id=f"HELP:{a.id}"))
    assert s.handler.handle_message(msg("location", id="w2", latitude=123.0, longitude=-76.5)) == "location_invalid"
    assert s.ledger.open_case_for(a.id).lat is None


# --- voice notes ----------------------------------------------------------------------------------------------


def voice_setup(**overrides):
    options = dict(media=FakeMedia(), audio_converter=FakeConverter(), transcriber=FakeTranscriber(), assistant=FakeAssistant())
    options.update(overrides)
    return Setup(**options)


def test_a_voice_note_is_downloaded_converted_transcribed_and_answered():
    s = voice_setup()
    outcome = s.handler.handle_message(msg("audio", media_id="A1", mime_type="audio/ogg; codecs=opus"))
    assert outcome == "answered"
    assert s.handler._media.requested == ["A1"]
    assert s.handler._converter.seen == [(b"ogg", "audio/ogg; codecs=opus")]
    assert s.handler._transcriber.calls == [(b"RIFFwav", "es")]
    assert s.handler._assistant.questions == ["hola, hay mucha agua en el rio"]
    assert "Respuesta del asistente." in s.replies[0] and "hay mucha agua" in s.replies[0]  # shows what it understood


def test_a_spoken_safe_command_is_recorded_like_text():
    s = voice_setup(transcriber=FakeTranscriber("Estoy a salvo"))
    a = alert(s.ledger)
    s.handler.handle_message(msg("audio", media_id="A1"))
    assert a.response is ResponseKind.SAFE


def test_without_an_audio_converter_it_asks_for_text():
    s = voice_setup(audio_converter=None)
    assert s.handler.handle_message(msg("audio", media_id="A1")) == "audio_unavailable"
    assert s.replies == [AUDIO_UNAVAILABLE_ES] == ["Por ahora no puedo escuchar audios, escríbame por favor."]


def test_without_a_transcriber_it_asks_for_text():
    s = voice_setup(transcriber=None)
    assert s.handler.handle_message(msg("audio", media_id="A1")) == "audio_unavailable"
    assert s.replies == [AUDIO_UNAVAILABLE_ES]


@pytest.mark.parametrize("override", [{"media": FakeMedia(fail=True)}, {"audio_converter": FakeConverter(fail=True)}])
def test_a_download_or_decode_failure_asks_for_text_and_never_raises(override):
    s = voice_setup(**override)
    assert s.handler.handle_message(msg("audio", media_id="A1")) == "audio_failed"
    assert s.replies and "escr" in s.replies[0].lower()


def test_an_empty_transcript_asks_to_repeat():
    s = voice_setup(transcriber=FakeTranscriber("  "))
    assert s.handler.handle_message(msg("audio", media_id="A1")) == "audio_empty"
    assert s.handler._assistant.questions == []


# --- video ----------------------------------------------------------------------------------------------------


def test_a_video_is_stored_and_opens_a_watcher_report_case_without_claiming_a_measurement():
    s = Setup(media=FakeMedia(b"video", "video/mp4"))
    a = alert(s.ledger)
    outcome = s.handler.handle_message(msg("video", media_id="V1", mime_type="video/mp4", caption="el río sube"))
    assert outcome == "video_case"
    assert s.clips == [(b"video", "video/mp4")]
    (case,) = s.ledger.list_cases()
    assert case.kind == "watcher_report" and case.household_id == "hh-1" and case.alert_id == a.id
    assert "clip-1" in case.note and "not measured" in case.note
    reply = s.replies[0].lower()
    assert "operador" in reply and "no" in reply and "medi" in reply  # says it was NOT measured


def test_a_video_from_an_unknown_phone_still_opens_a_case_without_storing_the_number():
    s = Setup(media=FakeMedia(b"video", "video/mp4"), is_allowed=lambda n: True)
    s.handler.handle_message(msg("video", sender="51911111111", media_id="V1"))
    (case,) = s.ledger.list_cases()
    assert case.household_id == "unregistered" and "51911111111" not in case.note


def test_a_failed_video_download_still_opens_the_case_with_the_media_reference():
    s = Setup(media=FakeMedia(fail=True))
    assert s.handler.handle_message(msg("video", media_id="V1")) == "video_case"
    (case,) = s.ledger.list_cases()
    assert "V1" in case.note and s.clips == []


# --- other types, security, robustness -------------------------------------------------------------------------


def test_unsupported_types_get_a_short_explanation():
    s = Setup()
    assert s.handler.handle_message(msg("image", media_id="I1")) == "unsupported"
    assert s.replies and "texto" in s.replies[0].lower()


def test_senders_outside_the_allowlist_are_ignored_silently():
    assistant = FakeAssistant()
    s = Setup(assistant=assistant, is_allowed=lambda number: False)
    assert s.handler.handle_message(msg(text="hola")) == "ignored_not_allowed"
    assert s.sent.messages == [] and assistant.questions == []


def test_duplicate_deliveries_are_processed_once():
    s = Setup()
    a = alert(s.ledger)
    first = s.handler.handle_message(msg("button_reply", id="w1", button_id=f"HELP:{a.id}"))
    second = s.handler.handle_message(msg("button_reply", id="w1", button_id=f"HELP:{a.id}"))
    assert (first, second) == ("recorded_help", "duplicate")
    assert len(s.ledger.list_cases()) == 1 and len(s.sent.messages) == 1


def test_a_sender_without_a_phone_never_crashes_and_gets_no_reply():
    s = Setup()
    assert s.handler.handle_message(msg(text="hola", sender="")) == "no_sender"
    assert s.sent.messages == []


def test_a_failing_reply_is_logged_and_does_not_raise():
    class Broken:
        def send(self, recipient, text):
            raise MessageError("outside window", code=131047)

    s = Setup(messaging=Broken())
    a = alert(s.ledger)
    assert s.handler.handle_message(msg("button_reply", button_id=f"SAFE:{a.id}")) == "recorded_safe"
    assert a.response is ResponseKind.SAFE  # the answer is recorded even when the confirmation cannot be sent
    assert any("131047" in e[3] for e in s.log.events)


def test_events_are_logged_with_the_phone_not_the_text():
    s = Setup()
    s.handler.handle_message(msg(text="mi secreto"))
    assert s.log.events and all("mi secreto" not in e[3] for e in s.log.events)


# --- delivery statuses --------------------------------------------------------------------------------------------


def test_statuses_update_the_alert_found_by_its_message_id_and_never_go_backwards():
    s = Setup()
    a = alert(s.ledger, message_id="wamid.OUT")
    a.message_status = "sent"
    assert s.handler.handle_status(DeliveryStatus("wamid.OUT", "delivered", WA)) is True
    assert a.message_status == "delivered"
    s.handler.handle_status(DeliveryStatus("wamid.OUT", "read", WA))
    s.handler.handle_status(DeliveryStatus("wamid.OUT", "delivered", WA))
    assert a.message_status == "read"


def test_a_failed_status_is_recorded_with_the_error_code():
    s = Setup()
    a = alert(s.ledger, message_id="wamid.OUT")
    s.handler.handle_status(DeliveryStatus("wamid.OUT", "failed", WA, error_code=131047, error_title="Re-engagement message"))
    assert a.message_status == "failed" and "131047" in a.message_note


def test_an_unmapped_status_is_ignored():
    s = Setup()
    assert s.handler.handle_status(DeliveryStatus("wamid.NOPE", "read", WA)) is False

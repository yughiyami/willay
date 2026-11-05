"""Use case: what Willay does with each message a household sends on WhatsApp, and with delivery statuses.

Button replies and the text commands 1/2 record the household's answer (same `record_response` as the call key press).
A location pin is attached to the household's open operator case. A voice note is converted, transcribed with Voice
Transcribe and then handled like text. A video opens a `watcher_report` case for an operator: it is stored but NEVER
measured here, and the reply says so. Other text is answered by the citizen assistant (no tools, guarded) or, without a
model, by a help menu. Only allow-listed senders are served, so a stranger cannot spend model budget.

Nothing here raises on bad input: a failure is logged and answered politely. The handler is synchronous; the HTTP layer
runs it in a background task so the webhook can answer Meta immediately.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections import OrderedDict
from typing import Callable, Protocol, Sequence

from willay.application.dispatch_alerts import UnknownAlert, record_response
from willay.domain.dispatch import Household, HouseholdAlert, OperatorCase, ResponseKind, phone_digits
from willay.domain.entities import AlertTier
from willay.ports.alert_ledger import AlertLedgerPort
from willay.ports.messaging import (
    AudioConverterPort,
    DeliveryStatus,
    EventLogPort,
    InboundMessage,
    MediaDownloadPort,
    MessagingPort,
)

MENU_ES = (
    "Hola, soy el asistente de Willay Relaves. Responda 1 si está a salvo o 2 si necesita ayuda. También puede "
    "compartir su ubicación o enviar una nota de voz. Willay complementa a SENAMHI e INDECI (INDECI: 115)."
)
SAFE_REPLY_ES = "Gracias, registramos que está a salvo."
HELP_REPLY_ES = "Un operador fue notificado. Si puede, comparta su ubicación."
NO_ALERT_ES = "No encontramos una alerta activa para su número. Si necesita ayuda urgente, llame a INDECI (115)."
UNKNOWN_ALERT_ES = "No encontramos esa alerta. Escriba «hola» para ver las opciones."
TEST_ACK_ES = "Prueba recibida: el canal de WhatsApp de Willay funciona."
LOCATION_OK_ES = "Gracias, un operador verá su ubicación."
LOCATION_NO_CASE_ES = "Recibimos su ubicación, pero no hay un caso abierto con su número. Si necesita ayuda, responda 2."
LOCATION_INVALID_ES = "No pudimos leer esa ubicación. Intente compartirla de nuevo."
AUDIO_UNAVAILABLE_ES = "Por ahora no puedo escuchar audios, escríbame por favor."
AUDIO_FAILED_ES = "No pude procesar su audio. Escríbame por favor."
AUDIO_EMPTY_ES = "No pude entender su audio. ¿Puede repetirlo o escribirlo?"
VIDEO_ES = "Recibimos su video. Todavía no lo hemos medido: un operador lo revisará. Si puede, comparta su ubicación."
UNSUPPORTED_ES = "Por ahora solo puedo leer texto, audios cortos, ubicaciones y videos. Escríbame por favor."

MAX_ANSWER_CHARS = 600
MAX_AUDIO_BYTES = 5_000_000
MAX_VIDEO_BYTES = 40_000_000
SEEN_LIMIT = 5000  # Meta retries a webhook for up to 7 days: remember the recent message ids

_SAFE = {"1", "a salvo", "estoy a salvo", "si estoy a salvo", "si a salvo"}
_HELP = {"2", "ayuda", "necesito ayuda", "auxilio", "socorro"}
_GREETINGS = {"hola", "holi", "buenas", "buenos dias", "buenas tardes", "buenas noches", "menu", "inicio", "hi", "hello"}


class AssistantPort(Protocol):
    def answer(self, text: str) -> str: ...


class TranscriberPort(Protocol):
    def transcribe(self, audio: bytes, language: str = "es") -> str: ...


def normalize_text(text: str) -> str:
    """Lower-case, no accents, no punctuation, single spaces: 'Sí, estoy a salvo.' -> 'si estoy a salvo'."""
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(ch for ch in plain if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", plain)).strip()


def _cap(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class WhatsAppInboundHandler:
    def __init__(
        self,
        *,
        ledger: AlertLedgerPort,
        messaging: MessagingPort,
        households: Sequence[Household] = (),
        is_allowed: Callable[[str], bool] = lambda number: True,
        media: MediaDownloadPort | None = None,
        audio_converter: AudioConverterPort | None = None,
        transcriber: TranscriberPort | None = None,
        assistant: AssistantPort | None = None,
        clip_sink: Callable[[bytes, str], str] | None = None,
        log: EventLogPort | None = None,
    ) -> None:
        self._ledger = ledger
        self._messaging = messaging
        self._households = tuple(households)
        self._is_allowed = is_allowed
        self._media = media
        self._converter = audio_converter
        self._transcriber = transcriber
        self._assistant = assistant
        self._clip_sink = clip_sink
        self._log = log
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    # --- plumbing -----------------------------------------------------------------------------------------

    def _record(self, direction: str, kind: str, phone: str, detail: str) -> None:
        if self._log is not None:
            self._log.record(direction, kind, phone, detail)

    def _first_time(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = None
            while len(self._seen) > SEEN_LIMIT:
                self._seen.popitem(last=False)
            return True

    def _reply(self, sender: str, text: str) -> None:
        try:
            self._messaging.send("+" + sender, text)
        except Exception as exc:  # the answer is already recorded; a failed confirmation must not undo it
            code = getattr(exc, "code", None)
            self._record("out", "reply", sender, f"reply failed: {type(exc).__name__}" + (f" (code {code})" if code else ""))

    def _alerts_of(self, sender: str) -> list[HouseholdAlert]:
        return [a for a in self._ledger.list_alerts() if phone_digits(a.household.phone) == sender]

    def _household_of(self, sender: str) -> Household | None:
        return next((h for h in self._households if phone_digits(h.phone) == sender), None)

    # --- messages -------------------------------------------------------------------------------------------

    def handle_message(self, msg: InboundMessage) -> str:
        """Process one inbound message and return a short outcome tag (also useful for tests and logs)."""
        if not self._first_time(msg.id):
            return "duplicate"
        if not msg.sender:
            self._record("in", msg.type, "", "message without a phone number (user id only): not answered")
            return "no_sender"
        if not self._is_allowed("+" + msg.sender):
            self._record("in", msg.type, msg.sender, "ignored: sender is not on the allow-list")
            return "ignored_not_allowed"
        self._record("in", msg.type, msg.sender, "received")
        try:
            outcome = self._route(msg)
        except Exception as exc:  # never let one message break the webhook worker
            self._record("in", msg.type, msg.sender, f"error while handling: {type(exc).__name__}")
            return "error"
        self._record("in", msg.type, msg.sender, f"handled: {outcome}")
        return outcome

    def _route(self, msg: InboundMessage) -> str:
        if msg.type == "button_reply":
            return self._button(msg)
        if msg.type == "text":
            return self._text(msg.sender, msg.text)
        if msg.type == "location":
            return self._location(msg)
        if msg.type == "audio":
            return self._audio(msg)
        if msg.type == "video":
            return self._video(msg)
        self._reply(msg.sender, UNSUPPORTED_ES)
        return "unsupported"

    def _button(self, msg: InboundMessage) -> str:
        prefix, _, alert_id = msg.button_id.partition(":")
        if prefix == "TEST":
            self._reply(msg.sender, TEST_ACK_ES)
            return "test_ack"
        if prefix not in ("SAFE", "HELP"):
            return "unknown_button"
        alert = self._ledger.get_alert(alert_id)
        if alert is None:
            self._reply(msg.sender, UNKNOWN_ALERT_ES)
            return "unknown_alert"
        if phone_digits(alert.household.phone) != msg.sender:
            self._record("in", "button_reply", msg.sender, "refused: the sender is not the household of this alert")
            return "sender_mismatch"
        return self._answer_alert(alert, ResponseKind.SAFE if prefix == "SAFE" else ResponseKind.NEEDS_HELP, msg.sender)

    def _answer_alert(self, alert: HouseholdAlert, response: ResponseKind, sender: str) -> str:
        try:
            record_response(alert.id, response, "whatsapp", self._ledger)
        except UnknownAlert:
            self._reply(sender, UNKNOWN_ALERT_ES)
            return "unknown_alert"
        safe = response is ResponseKind.SAFE
        self._reply(sender, SAFE_REPLY_ES if safe else HELP_REPLY_ES)
        return "recorded_safe" if safe else "recorded_help"

    def _text(self, sender: str, text: str, spoken: bool = False) -> str:
        words = normalize_text(text)
        command = ResponseKind.SAFE if words in _SAFE else ResponseKind.NEEDS_HELP if words in _HELP else None
        if command is not None:
            alerts = self._alerts_of(sender)
            if not alerts:
                self._reply(sender, NO_ALERT_ES)
                return "no_alert"
            return self._answer_alert(alerts[-1], command, sender)
        if words in _GREETINGS or not words:
            self._reply(sender, MENU_ES)
            return "menu"
        if self._assistant is None:
            self._reply(sender, MENU_ES)
            return "menu"
        try:
            answer = _cap(self._assistant.answer(text))
        except Exception as exc:  # budget, network or model errors: fall back to the static menu
            self._record("out", "answer", sender, f"assistant failed: {type(exc).__name__}")
            self._reply(sender, MENU_ES)
            return "menu"
        self._reply(sender, f"Entendí: «{_cap(text, 150)}». {answer}" if spoken else answer)
        return "answered"

    def _location(self, msg: InboundMessage) -> str:
        lat, lon = msg.latitude, msg.longitude
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self._reply(msg.sender, LOCATION_INVALID_ES)
            return "location_invalid"
        phones = {h.id: phone_digits(h.phone) for h in self._households}
        phones.update({a.household.id: phone_digits(a.household.phone) for a in self._ledger.list_alerts()})
        cases = [c for c in self._ledger.list_cases() if c.status == "open" and phones.get(c.household_id) == msg.sender]
        if not cases:
            self._reply(msg.sender, LOCATION_NO_CASE_ES)
            return "location_no_case"
        case = cases[-1]
        case.lat, case.lon = lat, lon
        case.location_note = f"shared via WhatsApp: {_cap(msg.location_name, 120)}".rstrip(": ")
        self._reply(msg.sender, LOCATION_OK_ES)
        return "location_attached"

    def _audio(self, msg: InboundMessage) -> str:
        if self._media is None or self._converter is None or self._transcriber is None or not msg.media_id:
            self._reply(msg.sender, AUDIO_UNAVAILABLE_ES)
            return "audio_unavailable"
        try:
            data, mime = self._media.download_media(msg.media_id, max_bytes=MAX_AUDIO_BYTES)
            wav = self._converter.to_wav(data, mime or msg.mime_type)
            transcript = self._transcriber.transcribe(wav, "es").strip()
        except Exception as exc:
            self._record("in", "audio", msg.sender, f"voice note failed: {type(exc).__name__}")
            self._reply(msg.sender, AUDIO_FAILED_ES)
            return "audio_failed"
        if not transcript:
            self._reply(msg.sender, AUDIO_EMPTY_ES)
            return "audio_empty"
        return self._text(msg.sender, transcript, spoken=True)

    def _video(self, msg: InboundMessage) -> str:
        stored = ""
        if self._media is not None and msg.media_id:
            try:
                data, mime = self._media.download_media(msg.media_id, max_bytes=MAX_VIDEO_BYTES)
                if self._clip_sink is not None:
                    stored = f"stored as {self._clip_sink(data, mime or msg.mime_type)}, "
            except Exception as exc:
                self._record("in", "video", msg.sender, f"video download failed: {type(exc).__name__}")
        alerts = self._alerts_of(msg.sender)
        alert = alerts[-1] if alerts else None
        household = alert.household if alert else self._household_of(msg.sender)
        caption = f" caption: {_cap(msg.caption, 120)}." if msg.caption else ""
        self._ledger.add_case(
            OperatorCase(
                id=self._ledger.new_id("case"),
                alert_id=alert.id if alert else "",
                household_id=household.id if household else "unregistered",
                site_id=alert.site_id if alert else "",
                tier=alert.tier if alert else AlertTier.NONE,
                reason=ResponseKind.NEEDS_HELP,
                note=f"video received via WhatsApp ({stored}media ref {msg.media_id or 'none'}); not measured: an operator must review it.{caption}",
                kind="watcher_report",
            )
        )
        self._reply(msg.sender, VIDEO_ES)
        return "video_case"

    # --- delivery statuses ------------------------------------------------------------------------------------

    def handle_status(self, status: DeliveryStatus) -> bool:
        """Update the alert whose outbound message id matches. Statuses never move backwards (sent < delivered < read)."""
        alert = next((a for a in self._ledger.list_alerts() if a.message_id and a.message_id == status.message_id), None)
        if alert is None:
            return False
        order = {"sent": 1, "delivered": 2, "read": 3}
        if status.status == "failed":
            alert.message_status = "failed"
            alert.message_note = f"WhatsApp reported a delivery failure (code {status.error_code}: {status.error_title})".replace("None", "?")
            self._record("out", "status", status.recipient, f"failed: code {status.error_code}")
        elif alert.message_status != "template_only" and order.get(status.status, 0) > order.get(alert.message_status, 0):
            alert.message_status = status.status
            self._record("out", "status", status.recipient, status.status)
        return True

"""Use case: what Willay answers a citizen on a chat channel that has no phone number (web chat, Telegram).

Same behaviors as the WhatsApp handler, with the same guardrails (the assistant never names an alert tier or orders an
evacuation; every answer cites SENAMHI/INDECI): SAFE/HELP buttons and the commands 1/2 call `record_response`, a location
becomes the position of the household's open operator case, a voice note is converted, transcribed and answered like
text, and other text goes to the citizen assistant. The difference is the identity: a chat is not tied to a phone but to
the alerts that were bound to it when an alert was dispatched (`bound_alert_ids`, oldest first).

Pure and channel-neutral: every method returns `ChatReply` objects that the channel adapter renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from willay.application.dispatch_alerts import UnknownAlert, record_response
from willay.application.whatsapp_inbound import (
    AUDIO_EMPTY_ES,
    AUDIO_FAILED_ES,
    AUDIO_UNAVAILABLE_ES,
    HELP_REPLY_ES,
    LOCATION_INVALID_ES,
    MAX_AUDIO_BYTES,
    MENU_ES,
    NO_ALERT_ES,
    SAFE_REPLY_ES,
    UNKNOWN_ALERT_ES,
    AssistantPort,
    TranscriberPort,
    _cap,
    _GREETINGS,
    _HELP,
    _SAFE,
    normalize_text,
)
from willay.domain.dispatch import ResponseKind
from willay.ports.alert_ledger import AlertLedgerPort
from willay.ports.messaging import AudioConverterPort

WELCOME_ES = (
    "Hola, soy Willay Relaves, un sistema de alerta temprana por El Niño para relaves mineros y comunidades río abajo "
    "en la cuenca del Rímac. Willay complementa, no reemplaza, las alertas oficiales de SENAMHI, INDECI y ENFEN "
    "(INDECI: 115). Cuando haya una alerta le escribiré aquí y podrá responder «Estoy a salvo» o «Necesito ayuda». "
    "También puede compartir su ubicación, enviar una nota de voz o preguntarme cómo prepararse."
)
LOCATION_OK_ES = "Gracias, un operador verá su ubicación."
LOCATION_NO_CASE_ES = "Recibimos su ubicación, pero no hay un caso abierto. Si necesita ayuda, toque «Necesito ayuda»."
ALERT_FOOTER_ES = "Willay complementa a SENAMHI e INDECI (INDECI: 115)."


@dataclass(frozen=True)
class ChatReply:
    """One message from Willay: text and, optionally, reply buttons as (id, title) pairs."""

    text: str
    buttons: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AudioOutcome:
    transcript: str  # empty when nothing could be understood
    replies: list[ChatReply]


def is_greeting(text: str) -> bool:
    words = normalize_text(text)
    return not words or words in _GREETINGS or words in ("start", "comenzar", "empezar")


class CitizenChat:
    def __init__(
        self,
        *,
        ledger: AlertLedgerPort,
        source: str,
        assistant: AssistantPort | None = None,
        transcriber: TranscriberPort | None = None,
        audio_converter: AudioConverterPort | None = None,
    ) -> None:
        self._ledger = ledger
        self._source = source  # the response channel recorded in the ledger: "webchat" | "telegram"
        self._assistant = assistant
        self._transcriber = transcriber
        self._converter = audio_converter

    # --- answers ----------------------------------------------------------------------------------------------------

    def welcome(self) -> ChatReply:
        return ChatReply(WELCOME_ES)

    def _answer_alert(self, alert_id: str, response: ResponseKind) -> list[ChatReply]:
        try:
            record_response(alert_id, response, self._source, self._ledger)
        except UnknownAlert:
            return [ChatReply(UNKNOWN_ALERT_ES)]
        return [ChatReply(SAFE_REPLY_ES if response is ResponseKind.SAFE else HELP_REPLY_ES)]

    def button(self, bound_alert_ids: Sequence[str], button_id: str) -> list[ChatReply]:
        prefix, _, alert_id = button_id.partition(":")
        if prefix not in ("SAFE", "HELP"):
            return []
        if alert_id not in bound_alert_ids:  # a button from another chat or from before a demo reset
            return [ChatReply(UNKNOWN_ALERT_ES)]
        return self._answer_alert(alert_id, ResponseKind.SAFE if prefix == "SAFE" else ResponseKind.NEEDS_HELP)

    def text(self, bound_alert_ids: Sequence[str], text: str, spoken: bool = False) -> list[ChatReply]:
        words = normalize_text(text)
        command = ResponseKind.SAFE if words in _SAFE else ResponseKind.NEEDS_HELP if words in _HELP else None
        if command is not None:
            if not bound_alert_ids:
                return [ChatReply(NO_ALERT_ES)]
            return self._answer_alert(bound_alert_ids[-1], command)
        if is_greeting(text):
            return [ChatReply(WELCOME_ES)]
        if self._assistant is None:
            return [ChatReply(MENU_ES)]
        try:
            answer = _cap(self._assistant.answer(text))
        except Exception:  # budget, network or model errors: fall back to the static menu
            return [ChatReply(MENU_ES)]
        return [ChatReply(f"Entendí: «{_cap(text, 150)}». {answer}" if spoken else answer)]

    def location(self, bound_alert_ids: Sequence[str], lat: float | None, lon: float | None, label: str = "") -> list[ChatReply]:
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return [ChatReply(LOCATION_INVALID_ES)]
        mine = set(bound_alert_ids)
        cases = [c for c in self._ledger.list_cases() if c.status == "open" and c.alert_id in mine]
        if not cases:
            return [ChatReply(LOCATION_NO_CASE_ES)]
        case = cases[-1]
        case.lat, case.lon = lat, lon
        case.location_note = f"shared via {self._source}: {_cap(label, 120)}".rstrip(": ")
        return [ChatReply(LOCATION_OK_ES)]

    def audio(self, audio: bytes, mime: str) -> AudioOutcome:
        """Convert and transcribe a voice note. The caller then passes the transcript to `text(..., spoken=True)`."""
        if self._converter is None or self._transcriber is None:
            return AudioOutcome("", [ChatReply(AUDIO_UNAVAILABLE_ES)])
        if len(audio) > MAX_AUDIO_BYTES:
            return AudioOutcome("", [ChatReply(AUDIO_FAILED_ES)])
        try:
            wav = self._converter.to_wav(audio, mime)
            transcript = self._transcriber.transcribe(wav, "es").strip()
        except Exception:
            return AudioOutcome("", [ChatReply(AUDIO_FAILED_ES)])
        if not transcript:
            return AudioOutcome("", [ChatReply(AUDIO_EMPTY_ES)])
        return AudioOutcome(transcript, [])

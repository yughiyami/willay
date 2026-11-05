from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable


class MessagingPort(Protocol):
    """Outbound messaging channel (SMS, WhatsApp, radio gateway...)."""

    def send(self, recipient: str, text: str) -> None: ...


@dataclass(frozen=True)
class SendResult:
    """What an interactive send reports: the provider message id and whether a template replaced the text."""

    message_id: str | None = None
    used_template: bool = False


@runtime_checkable
class InteractiveMessagingPort(Protocol):
    """Optional capability of a channel: a text with up to three reply buttons, given as (id, title) pairs.

    A button press comes back as an inbound message carrying its id. Raises like `MessagingPort.send` on failure.
    """

    def send_buttons(self, recipient: str, body: str, buttons: Sequence[tuple[str, str]]) -> SendResult: ...


class MediaDownloadPort(Protocol):
    """Fetches a media file (voice note, video) that a user sent."""

    def download_media(self, media_id: str, max_bytes: int = ...) -> tuple[bytes, str]: ...


class AudioConverterPort(Protocol):
    """Turns a voice note (OGG/Opus...) into the 16 kHz mono 16-bit PCM WAV that speech-to-text accepts."""

    def to_wav(self, audio: bytes, mime: str) -> bytes: ...


class EventLogPort(Protocol):
    """Operator-facing trail of channel events. Never receives message text; the phone is masked by the adapter."""

    def record(self, direction: str, kind: str, phone: str, detail: str) -> None: ...


class MessageNotAllowed(Exception):
    """The recipient is not on the allow-list (or is not a valid number); nothing was sent."""


class MessageError(Exception):
    """The channel rejected or failed a send. `code` is the provider error code when there is one."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InboundMessage:
    """One message a user sent to the channel, in provider-neutral form.

    `sender` is the user's phone number as digits (WhatsApp wa_id, no '+'); it is empty when the provider only gave a
    user id (`user_id`). `type` is text, button_reply, location, audio, video, image or whatever else arrived.
    """

    id: str
    sender: str
    type: str
    text: str = ""
    button_id: str = ""
    latitude: float | None = None
    longitude: float | None = None
    location_name: str = ""
    media_id: str = ""
    mime_type: str = ""
    caption: str = ""
    user_id: str = ""


@dataclass(frozen=True)
class DeliveryStatus:
    """Delivery progress of an outbound message: sent, delivered, read or failed."""

    message_id: str
    status: str
    recipient: str = ""
    error_code: int | None = None
    error_title: str = ""

"""In-memory web chat: a WhatsApp-like conversation in the browser, with no external service.

Thread-safe store of sessions and messages (capped) plus the alert channel that delivers dispatched alerts to every
session. The browser polls for messages by sequence number. `direction` is from the citizen's point of view: "in" is
what the citizen sent, "out" is what Willay sent.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from willay.application.alert_fanout import CARD_CAPTION_ES
from willay.domain.dispatch import HouseholdAlert

MAX_SESSIONS = 20
MAX_MESSAGES = 300
MAX_MEDIA = 20
MAX_TEXT_CHARS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class WebChatMessage:
    seq: int
    direction: str  # in | out
    kind: str  # text | buttons | image | location | audio | system
    text: str | None = None
    buttons: list[dict] | None = None
    image_url: str | None = None
    lat: float | None = None
    lon: float | None = None
    ts: str = field(default_factory=_now)
    status: str | None = None  # sent | delivered | read

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "direction": self.direction,
            "kind": self.kind,
            "text": self.text,
            "buttons": self.buttons,
            "image_url": self.image_url,
            "lat": self.lat,
            "lon": self.lon,
            "ts": self.ts,
            "status": self.status,
        }


@dataclass
class WebChatSession:
    id: str
    display_name: str
    messages: list[WebChatMessage] = field(default_factory=list)
    next_seq: int = 1
    alert_ids: list[str] = field(default_factory=list)  # alerts bound to this chat, oldest first
    welcomed: bool = False


class WebChatStore:
    def __init__(self, max_sessions: int = MAX_SESSIONS, max_messages: int = MAX_MESSAGES) -> None:
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, WebChatSession] = OrderedDict()
        self._media: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._max_sessions, self._max_messages = max_sessions, max_messages
        self._visitors = 0

    # --- sessions -----------------------------------------------------------------------------------------------

    def create_session(self, name: str | None = None) -> WebChatSession:
        with self._lock:
            self._visitors += 1
            clean = " ".join((name or "").split())[:40]
            session = WebChatSession(id=secrets.token_urlsafe(9), display_name=clean or f"Visitante {self._visitors}")
            self._sessions[session.id] = session
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)  # forget the oldest conversation
            return session

    def get(self, session_id: str) -> WebChatSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def session_ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def bound_alerts(self, session_id: str) -> list[str]:
        with self._lock:
            session = self._sessions.get(session_id)
            return list(session.alert_ids) if session else []

    def bind_alert(self, session_id: str, alert_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and alert_id not in session.alert_ids:
                session.alert_ids.append(alert_id)

    def reset_bindings(self) -> None:
        """The demo reset wipes the ledger, so alert ids bound earlier no longer mean anything."""
        with self._lock:
            for session in self._sessions.values():
                session.alert_ids.clear()

    def first_contact(self, session_id: str) -> bool:
        """True exactly once per session: the welcome is sent on the first message."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.welcomed:
                return False
            session.welcomed = True
            return True

    # --- messages -----------------------------------------------------------------------------------------------

    def add(
        self,
        session_id: str,
        direction: str,
        kind: str,
        *,
        text: str | None = None,
        buttons: Sequence[tuple[str, str]] | None = None,
        image_url: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        status: str | None = None,
    ) -> WebChatMessage | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            message = WebChatMessage(
                seq=session.next_seq,
                direction=direction,
                kind=kind,
                text=text[:MAX_TEXT_CHARS] if text is not None else None,
                buttons=[{"id": i, "title": t} for i, t in buttons] if buttons else None,
                image_url=image_url,
                lat=lat,
                lon=lon,
                status=status,
            )
            session.next_seq += 1
            session.messages.append(message)
            del session.messages[: max(0, len(session.messages) - self._max_messages)]
            return message

    def messages_after(self, session_id: str, after: int = 0) -> list[dict] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return [m.to_dict() for m in session.messages if m.seq > after]

    def button_title(self, session_id: str, button_id: str) -> str | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            for message in reversed(session.messages):
                for button in message.buttons or ():
                    if button["id"] == button_id:
                        return button["title"]
            return None

    # --- media --------------------------------------------------------------------------------------------------

    def put_media(self, data: bytes, mime: str = "image/png") -> str:
        name = secrets.token_urlsafe(9) + (".png" if mime == "image/png" else ".bin")
        with self._lock:
            self._media[name] = (data, mime)
            while len(self._media) > MAX_MEDIA:
                self._media.popitem(last=False)
        return name

    def get_media(self, name: str) -> tuple[bytes, str] | None:
        with self._lock:
            return self._media.get(name)


class WebChatChannel:
    """Alert channel: delivers a dispatched alert to every web chat session."""

    name = "webchat"

    def __init__(self, store: WebChatStore, media_base: str = "/webchat/media") -> None:
        self._store = store
        self._media_base = media_base.rstrip("/")

    def subscriber_ids(self) -> list[str]:
        return self._store.session_ids()

    def deliver(self, subscriber_id: str, alert: HouseholdAlert, buttons: Sequence[tuple[str, str]], card_png: bytes | None) -> None:
        self._store.bind_alert(subscriber_id, alert.id)
        self._store.add(subscriber_id, "out", "buttons", text=alert.message_text, buttons=buttons)
        if card_png:
            name = self._store.put_media(card_png)
            self._store.add(subscriber_id, "out", "image", text=CARD_CAPTION_ES, image_url=f"{self._media_base}/{name}")

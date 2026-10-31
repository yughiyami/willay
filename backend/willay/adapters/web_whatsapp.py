"""WhatsApp-style messaging simulator: keeps the outgoing messages so the web UI can show them on a phone mock-up.

No message leaves the machine. Replies come back through POST /alerts/{id}/response.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SimulatedMessage:
    recipient: str
    text: str
    sent_at: str


class WebWhatsAppSimulator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: list[SimulatedMessage] = []

    def send(self, recipient: str, text: str) -> None:
        message = SimulatedMessage(recipient, text, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self._lock:
            self._messages.append(message)

    def messages_for(self, recipient: str) -> list[SimulatedMessage]:
        with self._lock:
            return [m for m in self._messages if m.recipient == recipient]

    def all_messages(self) -> list[SimulatedMessage]:
        with self._lock:
            return list(self._messages)

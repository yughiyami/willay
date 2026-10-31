from __future__ import annotations

from typing import Protocol


class MessagingPort(Protocol):
    """Outbound messaging channel (SMS, WhatsApp, radio gateway...)."""

    def send(self, recipient: str, text: str) -> None: ...

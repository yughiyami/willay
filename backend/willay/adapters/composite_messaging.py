"""Messaging that delivers on a real channel AND mirrors what was really sent into the web phone mock-up.

The mock-up (WebWhatsAppSimulator) keeps working when WhatsApp is live. A message that the real channel refused is
never mirrored as delivered: the error propagates so the alert is marked failed.
"""

from __future__ import annotations

from typing import Sequence

from willay.adapters.web_whatsapp import SimulatedMessage, WebWhatsAppSimulator
from willay.ports.messaging import MessagingPort, SendResult


class MirroredMessaging:
    def __init__(self, primary: MessagingPort, mirror: WebWhatsAppSimulator) -> None:
        self.primary = primary
        self.mirror = mirror

    def send(self, recipient: str, text: str) -> None:
        self.primary.send(recipient, text)
        self.mirror.send(recipient, text)

    def send_buttons(self, recipient: str, body: str, buttons: Sequence[tuple[str, str]]) -> SendResult:
        result = self.primary.send_buttons(recipient, body, buttons)  # type: ignore[attr-defined]
        shown = body if not result.used_template else "[Plantilla de WhatsApp enviada: el texto de la alerta no pudo entregarse]"
        self.mirror.send(recipient, shown + "\n" + "  ".join(f"[{title}]" for _, title in buttons))
        return result

    # --- the phone mock-up and the demo reset read the simulator through the same object ----------------------

    def reset(self) -> None:
        self.mirror.reset()

    def messages_for(self, recipient: str) -> list[SimulatedMessage]:
        return self.mirror.messages_for(recipient)

    def all_messages(self) -> list[SimulatedMessage]:
        return self.mirror.all_messages()

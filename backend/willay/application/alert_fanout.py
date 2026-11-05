"""Use case: after an alert dispatch, also deliver it to the chat channels that have no phone number (web chat, Telegram).

Each subscribed chat acts as one of the dispatched SYNTHETIC households (the presenter's household): the chat is bound
to one alert of the dispatch, so its SAFE/HELP answer goes through `record_response` and updates the operator console
exactly like the simulated phone. Alerts nobody has answered yet are used first, then those that did not answer a call.
A failure on one chat or channel never stops the others.
"""

from __future__ import annotations

from typing import Callable, Protocol, Sequence

from willay.application.dispatch_alerts import SAFE_BUTTON, HELP_BUTTON, DispatchResult
from willay.domain.dispatch import HouseholdAlert, ResponseKind
from willay.domain.entities import AlertTier

CARD_CAPTION_ES = "Tarjeta de evacuación (ejemplo ilustrativo). Siga siempre las indicaciones de INDECI y su municipalidad."


class AlertChannel(Protocol):
    name: str

    def subscriber_ids(self) -> list[str]: ...

    def deliver(self, subscriber_id: str, alert: HouseholdAlert, buttons: Sequence[tuple[str, str]], card_png: bytes | None) -> None: ...


def alert_buttons(alert: HouseholdAlert) -> list[tuple[str, str]]:
    return [(f"{prefix}:{alert.id}", title) for prefix, title in (SAFE_BUTTON, HELP_BUTTON)]


def _priority(alert: HouseholdAlert) -> int:
    if alert.response is None:
        return 0
    return 1 if alert.response is ResponseKind.NO_ANSWER else 2


class AlertFanout:
    def __init__(self, channels: Sequence[AlertChannel], card_provider: Callable[[], bytes | None] = lambda: None) -> None:
        self._channels = tuple(channels)
        self._card_provider = card_provider

    def __call__(self, result: DispatchResult) -> None:
        if not result.alerts:
            return
        pool = sorted(result.alerts, key=_priority)  # stable: keeps the dispatch order inside each group
        card = self._card_provider() if result.tier is AlertTier.EVACUATION else None
        index = 0
        for channel in self._channels:
            try:
                subscribers = channel.subscriber_ids()
            except Exception:
                continue
            for subscriber in subscribers:
                alert = pool[index % len(pool)]
                index += 1
                try:
                    channel.deliver(subscriber, alert, alert_buttons(alert), card)
                except Exception:  # one broken chat must not stop the rest
                    continue

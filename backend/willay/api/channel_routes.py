"""Wires the two extra citizen channels that do not need WhatsApp: the web chat (always on) and the Telegram bot.

Both reuse the same inbound logic (`CitizenChat`) and receive dispatched alerts through `AlertFanout`, which binds each
chat to one SYNTHETIC household alert so SAFE/HELP update the operator console like the simulated phone does.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import FastAPI

from willay.adapters.audio_convert import build_audio_converter
from willay.adapters.telegram_bot import TelegramApi, TelegramBot
from willay.adapters.webchat import WebChatChannel
from willay.api.dependencies import AppDependencies
from willay.api.webchat_routes import WebChatService, register_webchat_routes
from willay.application.alert_fanout import AlertChannel, AlertFanout
from willay.application.citizen_assistant import CitizenAssistant
from willay.application.citizen_chat import CitizenChat


def webchat_mode(deps: AppDependencies) -> str:
    return f"live ({len(deps.webchat.session_ids())} sessions)"


def telegram_mode(deps: AppDependencies) -> str:
    if deps.telegram is not None:
        return f"live ({deps.telegram.chat_count} chats)"
    return deps.telegram_mode


def register_chat_channels(app: FastAPI, deps: AppDependencies) -> None:
    converter = deps.audio_converter or build_audio_converter()

    def build_chat(source: str) -> CitizenChat:
        return CitizenChat(
            ledger=deps.ledger,
            source=source,
            assistant=CitizenAssistant(deps.agent_model) if deps.agent_model is not None else None,
            transcriber=deps.transcriber,  # type: ignore[arg-type]
            audio_converter=converter,  # type: ignore[arg-type]
        )

    channels: list[AlertChannel] = [WebChatChannel(deps.webchat)]  # type: ignore[list-item]
    register_webchat_routes(app, deps.webchat, WebChatService(deps.webchat, build_chat("webchat")))

    deps.telegram = None
    if deps.telegram_config is not None:
        deps.telegram = TelegramBot(
            deps.telegram_config, TelegramApi(deps.telegram_config.token, deps.telegram_http_client), build_chat("telegram")
        )
        channels.append(deps.telegram)  # type: ignore[arg-type]

    card_cache: list[bytes | None] = []

    def card_png() -> bytes | None:
        """The recorded evacuation card of the offline demo, when there is one (optional extra for the chats)."""
        if not card_cache:
            encoded = (deps.demo_recording or {}).get("card_png_base64")
            try:
                card_cache.append(base64.b64decode(encoded) if encoded else None)
            except (binascii.Error, ValueError):
                card_cache.append(None)
        return card_cache[0]

    deps.alert_fanout = AlertFanout(channels, card_png)

    @app.get("/telegram/status")
    def telegram_status() -> dict:
        if deps.telegram is None:
            return {"enabled": False, "chats": 0, "last_error": None, "mode": deps.telegram_mode}
        status = deps.telegram.status()
        return {"enabled": True, "chats": status["chats"], "chats_masked": status["chats_masked"], "last_error": status["last_error"], "mode": telegram_mode(deps)}

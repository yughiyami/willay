"""Telegram bot channel by long polling (getUpdates): no webhook and no tunnel, so it works from a laptop.

The bot token is a secret: it is only used to build request URLs, never logged, and every error text is masked.
Chats subscribe with /start (or are on TELEGRAM_ALLOWED_CHAT_IDS) and then receive dispatched alerts with inline
buttons. Inbound handling reuses `CitizenChat`, the same logic as the web chat.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import httpx

from willay.application.alert_fanout import CARD_CAPTION_ES
from willay.application.citizen_chat import ChatReply, CitizenChat, is_greeting
from willay.application.whatsapp_inbound import AUDIO_FAILED_ES
from willay.domain.dispatch import HouseholdAlert

API_ROOT = "https://api.telegram.org"
POLL_TIMEOUT_S = 25
MAX_CHATS = 10
MAX_VOICE_BYTES = 5_000_000
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_ALLOWED = "TELEGRAM_ALLOWED_CHAT_IDS"
NOT_SUBSCRIBED_ES = "Escriba /start para comenzar a recibir alertas de Willay."
FULL_ES = "Este bot ya tiene el máximo de chats de la demostración."


class TelegramError(RuntimeError):
    """A Telegram call failed. The message never contains the bot token."""


def mask_token(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


@dataclass(frozen=True)
class TelegramConfig:
    token: str = field(repr=False)  # repr=False: never printed in logs or tracebacks
    allowed_chat_ids: frozenset[int] = frozenset()
    max_chats: int = MAX_CHATS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "TelegramConfig | None":
        token = (env.get(ENV_TOKEN) or "").strip()
        if not token:
            return None
        allowed: set[int] = set()
        for part in (env.get(ENV_ALLOWED) or "").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                allowed.add(int(part))
        return cls(token=token, allowed_chat_ids=frozenset(allowed))


class TelegramApi:
    """Thin Bot API client over httpx. Raises TelegramError with a token-free message."""

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client or httpx.Client(timeout=15.0)

    def _fail(self, what: str) -> TelegramError:
        return TelegramError(mask_token(what, self._token))

    def call(self, method: str, _timeout: float = 15.0, **payload) -> object:
        try:
            response = self._client.post(f"{API_ROOT}/bot{self._token}/{method}", json=payload, timeout=_timeout)
        except httpx.HTTPError as exc:  # the exception text can carry the URL, and so the token: report the class only
            raise self._fail(f"{method}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise self._fail(f"{method}: HTTP {response.status_code} with a non-JSON body") from None
        if not body.get("ok"):
            raise self._fail(f"{method}: {body.get('error_code', response.status_code)} {str(body.get('description', ''))[:120]}")
        return body.get("result")

    def get_updates(self, offset: int | None, timeout: int = POLL_TIMEOUT_S) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self.call("getUpdates", _timeout=timeout + 15.0, **payload) or [])  # type: ignore[arg-type]

    def send_message(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]] = ()) -> None:
        payload: dict = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": title, "callback_data": bid} for bid, title in buttons]]}
        self.call("sendMessage", **payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def send_photo(self, chat_id: int, png: bytes, caption: str) -> None:
        try:
            response = self._client.post(
                f"{API_ROOT}/bot{self._token}/sendPhoto",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"photo": ("card.png", png, "image/png")},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise self._fail(f"sendPhoto: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise self._fail(f"sendPhoto: HTTP {response.status_code}")

    def download_file(self, file_id: str, max_bytes: int = MAX_VOICE_BYTES) -> bytes:
        info = self.call("getFile", file_id=file_id)
        path = (info or {}).get("file_path") if isinstance(info, dict) else None  # type: ignore[union-attr]
        if not path:
            raise self._fail("getFile: no file_path")
        if isinstance(info, dict) and int(info.get("file_size") or 0) > max_bytes:
            raise self._fail("getFile: the file is too large")
        try:
            response = self._client.get(f"{API_ROOT}/file/bot{self._token}/{path}", timeout=30.0)
        except httpx.HTTPError as exc:
            raise self._fail(f"download: {type(exc).__name__}") from None
        if response.status_code != 200 or len(response.content) > max_bytes:
            raise self._fail(f"download: HTTP {response.status_code}")
        return response.content


@dataclass
class _Chat:
    chat_id: int
    name: str = ""
    alert_ids: list[str] = field(default_factory=list)
    welcomed: bool = False


class TelegramBot:
    """Long-polling bot. `handle_update` is synchronous and testable; `start` runs the poll loop in a daemon thread."""

    name = "telegram"

    def __init__(self, config: TelegramConfig, api: TelegramApi, chat: CitizenChat, sleep=time.sleep) -> None:
        self._config, self._api, self._chat = config, api, chat
        self._sleep = sleep
        self._chats: dict[int, _Chat] = {}
        self._lock = threading.RLock()
        self._offset: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="willay-telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            self._api.call("deleteWebhook")  # getUpdates fails with 409 while a webhook is set
        except TelegramError as exc:
            self.last_error = str(exc)
        delay = 2.0
        while not self._stop.is_set():
            try:
                updates = self._api.get_updates(self._offset)
                delay = 2.0
                self.last_error = None
            except TelegramError as exc:
                self.last_error = str(exc)
                self._sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            for update in updates:
                self._offset = int(update.get("update_id", 0)) + 1
                try:
                    self.handle_update(update)
                except Exception as exc:  # one bad update must not stop the loop
                    self.last_error = f"update {update.get('update_id')}: {mask_token(type(exc).__name__, self._config.token)}"

    # --- status ---------------------------------------------------------------------------------------------------

    @property
    def chat_count(self) -> int:
        with self._lock:
            return len(self._chats)

    def status(self) -> dict:
        with self._lock:
            masked = [f"…{str(c)[-3:]}" for c in self._chats]
        return {"enabled": True, "chats": len(masked), "chats_masked": masked, "last_error": self.last_error}

    def reset_bindings(self) -> None:
        with self._lock:
            for chat in self._chats.values():
                chat.alert_ids.clear()

    # --- subscribers (alert channel) -----------------------------------------------------------------------------

    def subscriber_ids(self) -> list[str]:
        with self._lock:
            return [str(c) for c in self._chats]

    def deliver(self, subscriber_id: str, alert: HouseholdAlert, buttons: Sequence[tuple[str, str]], card_png: bytes | None) -> None:
        chat_id = int(subscriber_id)
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                return
            if alert.id not in chat.alert_ids:
                chat.alert_ids.append(alert.id)
        self._api.send_message(chat_id, alert.message_text, buttons)
        if card_png:
            try:
                self._api.send_photo(chat_id, card_png, CARD_CAPTION_ES)
            except TelegramError as exc:  # the card is optional
                self.last_error = str(exc)

    # --- inbound ---------------------------------------------------------------------------------------------------

    def _subscribe(self, chat_id: int, name: str, explicit: bool) -> _Chat | None:
        """Return the chat if it may be served. Allow-listed chats join on any message; others must send /start."""
        with self._lock:
            known = self._chats.get(chat_id)
            if known is not None:
                return known
            allowed = self._config.allowed_chat_ids
            if allowed and chat_id not in allowed:
                return None
            if not allowed and not explicit:
                return None
            if len(self._chats) >= self._config.max_chats:
                return None
            chat = _Chat(chat_id, name)
            self._chats[chat_id] = chat
            return chat

    def _bound(self, chat: _Chat) -> list[str]:
        with self._lock:
            return list(chat.alert_ids)

    def _reply(self, chat_id: int, replies: Sequence[ChatReply]) -> None:
        for reply in replies:
            try:
                self._api.send_message(chat_id, reply.text, reply.buttons)
            except TelegramError as exc:  # the answer is already recorded; a failed confirmation must not undo it
                self.last_error = str(exc)

    def handle_update(self, update: Mapping) -> str:
        callback = update.get("callback_query")
        if callback:
            return self._on_callback(callback)
        message = update.get("message")
        if not message:
            return "ignored"
        chat_id = int((message.get("chat") or {}).get("id", 0))
        if not chat_id:
            return "ignored"
        text = message.get("text") or ""
        sender = message.get("from") or {}
        name = str(sender.get("first_name") or "")[:40]
        command = (text.split() or [""])[0].lower().split("@")[0]
        is_start = command in ("/start", "/help")
        chat = self._subscribe(chat_id, name, explicit=is_start)
        if chat is None:
            known_full = len(self._chats) >= self._config.max_chats
            if not self._config.allowed_chat_ids:
                self._safe_send(chat_id, FULL_ES if (is_start and known_full) else NOT_SUBSCRIBED_ES)
            return "not_subscribed"
        return self._on_message(chat, message, text, is_start)

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self._api.send_message(chat_id, text)
        except TelegramError as exc:
            self.last_error = str(exc)

    def _on_message(self, chat: _Chat, message: Mapping, text: str, is_start: bool) -> str:
        first = not chat.welcomed
        chat.welcomed = True
        welcome = self._chat.welcome()
        if is_start:
            self._reply(chat.chat_id, [welcome])
            return "welcome"
        if message.get("location"):
            loc = message["location"]
            self._reply(chat.chat_id, self._chat.location(self._bound(chat), loc.get("latitude"), loc.get("longitude"), "Telegram"))
            return "location"
        voice = message.get("voice") or message.get("audio")
        if voice:
            return self._on_voice(chat, voice, first)
        if text:
            replies = self._chat.text(self._bound(chat), text)
            if first and not is_greeting(text) and replies and replies[0].text != welcome.text:
                replies = [welcome, *replies]
            self._reply(chat.chat_id, replies)
            return "text"
        self._reply(chat.chat_id, [ChatReply("Por ahora solo puedo leer texto, audios cortos y ubicaciones.")])
        return "unsupported"

    def _on_voice(self, chat: _Chat, voice: Mapping, first: bool) -> str:
        try:
            data = self._api.download_file(str(voice.get("file_id", "")))
        except TelegramError as exc:
            self.last_error = str(exc)
            self._reply(chat.chat_id, [ChatReply(AUDIO_FAILED_ES)])
            return "audio_failed"
        outcome = self._chat.audio(data, str(voice.get("mime_type") or "audio/ogg"))
        if outcome.transcript:
            self._reply(chat.chat_id, self._chat.text(self._bound(chat), outcome.transcript, spoken=True))
            return "audio"
        self._reply(chat.chat_id, outcome.replies)
        return "audio_failed"

    def _on_callback(self, callback: Mapping) -> str:
        callback_id = str(callback.get("id", ""))
        message = callback.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or (callback.get("from") or {}).get("id") or 0)
        data = str(callback.get("data") or "")
        with self._lock:
            chat = self._chats.get(chat_id)
        if chat is None:
            self._safe_answer(callback_id, "Escriba /start")
            return "not_subscribed"
        replies = self._chat.button(self._bound(chat), data)
        self._safe_answer(callback_id, "Listo")
        self._reply(chat_id, replies)
        return "button"

    def _safe_answer(self, callback_id: str, text: str) -> None:
        try:
            self._api.answer_callback(callback_id, text[:150])
        except TelegramError as exc:
            self.last_error = str(exc)

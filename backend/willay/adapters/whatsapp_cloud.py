"""WhatsApp channel through the official Meta WhatsApp Business Cloud API (Graph API). Off unless explicitly configured.

`WhatsAppCloudMessenger` implements `MessagingPort.send` plus the optional `send_buttons` (reply buttons),
`send_template` and `download_media`. It also validates the webhook (`X-Hub-Signature-256`, verify token).

Safety: numbers outside the allow-list (`WILLAY_MESSAGE_ALLOWLIST`, else `WILLAY_CALL_ALLOWLIST`) are refused before any
request. The token never appears in a URL, log line, error or `repr`. Outside the 24 h customer-service window
(error 131047) a text or button send falls back to the `hello_world` template and says so: the alert text itself was
NOT delivered in that case. Error 131030 means the recipient is not on the Meta test number's recipient list.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import httpx

from willay.adapters.asterisk_ari import ENV_ALLOWLIST, normalize_number, parse_allowlist
from willay.domain.dispatch import mask_phone, phone_digits
from willay.ports.messaging import DeliveryStatus, InboundMessage, MessageError, MessageNotAllowed, SendResult

ENV_TOKEN = "WHATSAPP_TOKEN"
ENV_PHONE_NUMBER_ID = "WHATSAPP_PHONE_NUMBER_ID"
ENV_VERIFY_TOKEN = "WHATSAPP_VERIFY_TOKEN"
ENV_APP_SECRET = "WHATSAPP_APP_SECRET"
ENV_GRAPH_VERSION = "WHATSAPP_GRAPH_VERSION"
ENV_MESSAGE_ALLOWLIST = "WILLAY_MESSAGE_ALLOWLIST"
DEFAULT_GRAPH_VERSION = "v26.0"
GRAPH_ROOT = "https://graph.facebook.com"

OUTSIDE_WINDOW = 131047  # re-engagement message: the 24 h window is closed, only templates may be sent
NOT_IN_TEST_LIST = 131030  # recipient phone number not in the allowed list of the test number
FALLBACK_TEMPLATE, FALLBACK_LANGUAGE = "hello_world", "en_US"
MAX_BODY_CHARS, MAX_BUTTONS, MAX_BUTTON_ID_CHARS, MAX_BUTTON_TITLE_CHARS = 1024, 3, 256, 20
MAX_MEDIA_BYTES = 40_000_000
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_VERSION = re.compile(r"^v\d{1,3}\.\d{1,2}$")
_DIGITS = re.compile(r"^\d{1,32}$")


class MissingWhatsAppConfigError(RuntimeError):
    """Required WhatsApp variables are missing (names only; values are never reported)."""


@dataclass(frozen=True)
class WhatsAppConfig:
    token: str = field(repr=False)
    phone_number_id: str
    verify_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    allowlist: frozenset[str]
    graph_version: str = DEFAULT_GRAPH_VERSION
    timeout_s: float = 15.0

    def __post_init__(self) -> None:
        if not _DIGITS.match(self.phone_number_id):
            raise ValueError(f"{ENV_PHONE_NUMBER_ID} must be the numeric phone number id from Meta")
        if not _VERSION.match(self.graph_version):
            raise ValueError(f"{ENV_GRAPH_VERSION} must look like v26.0")
        if self.timeout_s <= 0:
            raise ValueError("the WhatsApp timeout must be > 0")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "WhatsAppConfig":
        names = (ENV_TOKEN, ENV_PHONE_NUMBER_ID, ENV_VERIFY_TOKEN, ENV_APP_SECRET)
        missing = [n for n in names if not (environ.get(n) or "").strip()]
        if missing:
            raise MissingWhatsAppConfigError("missing environment variables: " + ", ".join(missing))
        version = (environ.get(ENV_GRAPH_VERSION) or DEFAULT_GRAPH_VERSION).strip()
        if version[:1].isdigit():
            version = "v" + version
        raw_allowlist = environ.get(ENV_MESSAGE_ALLOWLIST)
        if raw_allowlist is None or not raw_allowlist.strip():
            raw_allowlist = environ.get(ENV_ALLOWLIST)
        return cls(
            token=environ[ENV_TOKEN].strip(),
            phone_number_id=environ[ENV_PHONE_NUMBER_ID].strip(),
            verify_token=environ[ENV_VERIFY_TOKEN].strip(),
            app_secret=environ[ENV_APP_SECRET].strip(),
            allowlist=parse_allowlist(raw_allowlist),
            graph_version=version,
        )


class WhatsAppEventLog:
    """Bounded, thread-safe trail of channel events for the operator console. Phones are masked; no message text."""

    def __init__(self, max_events: int = 200) -> None:
        self._events: deque[dict] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def record(self, direction: str, kind: str, phone: str, detail: str) -> None:
        event = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "direction": direction,
            "kind": kind,
            "phone": mask_phone(phone_digits(phone)) if phone_digits(phone) else "",
            "detail": detail,
        }
        with self._lock:
            self._events.append(event)

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(reversed(self._events))[:limit]


def to_wa_id(number: str) -> str:
    """'+51 900 000 001' -> '51900000001': the form the Cloud API takes in `to` (no plus sign)."""
    return phone_digits(number)


class WhatsAppCloudMessenger:
    def __init__(self, config: WhatsAppConfig, client: httpx.Client | None = None, log: WhatsAppEventLog | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=config.timeout_s)
        self.log = log or WhatsAppEventLog()

    def __repr__(self) -> str:
        return f"WhatsAppCloudMessenger(phone_number_id={self._config.phone_number_id!r}, graph={self._config.graph_version})"

    @property
    def allowlist(self) -> frozenset[str]:
        return self._config.allowlist

    @property
    def graph_version(self) -> str:
        return self._config.graph_version

    # --- webhook side -------------------------------------------------------------------------------------

    def validate_signature(self, raw_body: bytes, header: str | None) -> bool:
        """X-Hub-Signature-256 = 'sha256=' + hex HMAC-SHA256 of the RAW request body keyed with the app secret."""
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(self._config.app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header.removeprefix("sha256="))

    def verify_token_matches(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self._config.verify_token.encode(), token.encode())  # type: ignore[union-attr]

    # --- helpers --------------------------------------------------------------------------------------------

    def is_allowed(self, number: str) -> bool:
        digits = to_wa_id(number)
        return bool(digits) and ("+" + digits) in self._config.allowlist

    def _recipient(self, number: str) -> str:
        if not _E164.match("+" + to_wa_id(number)) or not self.is_allowed(number):
            raise MessageNotAllowed(f"the number is not on the allow-list ({ENV_MESSAGE_ALLOWLIST} or {ENV_ALLOWLIST})")
        return to_wa_id(number)

    def _redact(self, text: str) -> str:
        return text.replace(self._config.token, "***")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.token}"}

    def _url(self, path: str) -> str:
        return f"{GRAPH_ROOT}/{self._config.graph_version}/{path}"

    def _error(self, response: httpx.Response) -> MessageError:
        code: int | None = None
        detail = ""
        try:
            error = response.json().get("error") or {}
            code = int(error["code"]) if error.get("code") is not None else None
            detail = self._redact(str(error.get("message", "")))[:160]
        except (ValueError, AttributeError, TypeError):
            pass
        text = f"WhatsApp rejected the request (HTTP {response.status_code}" + (f", code {code}" if code is not None else "") + ")"
        return MessageError(text + (f": {detail}" if detail else ""), code=code)

    def _post_message(self, payload: dict[str, Any]) -> str | None:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        try:
            response = self._client.post(self._url(f"{self._config.phone_number_id}/messages"), headers=self._headers(), json=body, timeout=self._config.timeout_s)
        except httpx.HTTPError as exc:
            raise MessageError(f"the WhatsApp request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        try:
            messages = response.json().get("messages") or []
            return str(messages[0]["id"]) if messages else None
        except (ValueError, AttributeError, KeyError, IndexError, TypeError):
            return None

    def _send_with_fallback(self, number: str, kind: str, payload: dict[str, Any]) -> SendResult:
        to = self._recipient(number)
        try:
            message_id = self._post_message({"to": to, **payload})
        except MessageError as exc:
            if exc.code == NOT_IN_TEST_LIST:
                self.log.record("out", kind, to, f"failed: code {NOT_IN_TEST_LIST}, recipient is not in the Meta test number's recipient list")
            if exc.code != OUTSIDE_WINDOW:
                if exc.code != NOT_IN_TEST_LIST:
                    self.log.record("out", kind, to, f"failed: {exc}")
                raise
            self.log.record("out", kind, to, f"code {OUTSIDE_WINDOW}: outside the 24 h window, sending template {FALLBACK_TEMPLATE} instead (the text was NOT delivered)")
            template = self._template_payload(to, FALLBACK_TEMPLATE, FALLBACK_LANGUAGE)
            try:
                template_id = self._post_message(template)
            except MessageError as second:
                self.log.record("out", "template", to, f"failed: {second}")
                raise
            return SendResult(template_id, used_template=True)
        self.log.record("out", kind, to, "sent")
        return SendResult(message_id)

    @staticmethod
    def _template_payload(to: str, name: str, language: str) -> dict[str, Any]:
        return {"to": to, "type": "template", "template": {"name": name, "language": {"code": language}}}

    # --- public port ---------------------------------------------------------------------------------------

    def send(self, recipient: str, text: str) -> None:
        self.send_text(recipient, text)

    def send_text(self, recipient: str, text: str) -> SendResult:
        return self._send_with_fallback(recipient, "text", {"type": "text", "text": {"preview_url": False, "body": text}})

    def send_buttons(self, recipient: str, body: str, buttons: Sequence[tuple[str, str]]) -> SendResult:
        buttons = list(buttons)
        if not 1 <= len(buttons) <= MAX_BUTTONS:
            raise ValueError(f"a button message needs 1 to {MAX_BUTTONS} buttons")
        if len({b[0] for b in buttons}) != len(buttons) or len({b[1] for b in buttons}) != len(buttons):
            raise ValueError("button ids and titles must be unique")
        for button_id, title in buttons:
            if not button_id or len(button_id) > MAX_BUTTON_ID_CHARS:
                raise ValueError(f"a button id must be 1 to {MAX_BUTTON_ID_CHARS} characters")
            if not title or len(title) > MAX_BUTTON_TITLE_CHARS:
                raise ValueError(f"a button title must be 1 to {MAX_BUTTON_TITLE_CHARS} characters")
        interactive = {
            "type": "button",
            "body": {"text": body[:MAX_BODY_CHARS]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in buttons]},
        }
        return self._send_with_fallback(recipient, "buttons", {"type": "interactive", "interactive": interactive})

    def send_template(self, recipient: str, name: str, lang: str = FALLBACK_LANGUAGE) -> SendResult:
        to = self._recipient(recipient)
        try:
            message_id = self._post_message(self._template_payload(to, name, lang))
        except MessageError as exc:
            self.log.record("out", "template", to, f"failed: {exc}")
            raise
        self.log.record("out", "template", to, f"sent template {name}")
        return SendResult(message_id)

    def download_media(self, media_id: str, max_bytes: int = MAX_MEDIA_BYTES) -> tuple[bytes, str]:
        """Two steps: GET /{media-id} gives a short-lived URL, then GET the URL with the same bearer token."""
        if not _DIGITS.match(media_id or ""):
            raise MessageError("invalid media id")
        try:
            meta = self._client.get(self._url(media_id), headers=self._headers(), timeout=self._config.timeout_s)
            if meta.status_code >= 400:
                raise self._error(meta)
            info = meta.json()
            url, mime = str(info.get("url", "")), str(info.get("mime_type", ""))
            if not url.startswith("https://"):
                raise MessageError("WhatsApp returned no usable media URL")
            if int(info.get("file_size") or 0) > max_bytes:
                raise MessageError("the media file is too large")
            data = self._client.get(url, headers=self._headers(), timeout=self._config.timeout_s)
        except httpx.HTTPError as exc:
            raise MessageError(f"the media download failed: {type(exc).__name__}") from exc
        except (ValueError, TypeError) as exc:
            raise MessageError("WhatsApp returned an unreadable media description") from exc
        if data.status_code >= 400:
            raise self._error(data)
        if len(data.content) > max_bytes:
            raise MessageError("the media file is too large")
        content_type = data.headers.get("content-type", "").split(";")[0].strip()
        return data.content, content_type or mime


# --- webhook payload parsing (pure) ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _parse_message(raw: dict) -> InboundMessage | None:
    message_id = str(raw.get("id") or "")
    if not message_id:
        return None
    kind = str(raw.get("type") or "unknown")
    sender = phone_digits(str(raw.get("from") or ""))
    user_id = str(raw.get("from_user_id") or raw.get("user_id") or "")
    fields: dict[str, Any] = {}
    if kind == "text":
        fields["text"] = str(_as_dict(raw.get("text")).get("body") or "")
    elif kind == "interactive":
        interactive = _as_dict(raw.get("interactive"))
        picked = _as_dict(interactive.get("button_reply") or interactive.get("list_reply"))
        kind = "button_reply"
        fields["button_id"] = str(picked.get("id") or "")
    elif kind == "location":
        place = _as_dict(raw.get("location"))
        try:
            fields.update(latitude=float(place["latitude"]), longitude=float(place["longitude"]))
        except (KeyError, TypeError, ValueError):
            pass
        fields["location_name"] = " ".join(str(p) for p in (place.get("name"), place.get("address")) if p)
    elif kind in ("audio", "video", "image", "document", "sticker"):
        media = _as_dict(raw.get(kind))
        fields.update(media_id=str(media.get("id") or ""), mime_type=str(media.get("mime_type") or ""), caption=str(media.get("caption") or ""))
    return InboundMessage(id=message_id, sender=sender, type=kind, user_id=user_id, **fields)


def parse_webhook_payload(payload: Any) -> tuple[list[InboundMessage], list[DeliveryStatus]]:
    """Every message and delivery status in a webhook POST. Walks all entry[].changes[]; never raises on odd input."""
    messages: list[InboundMessage] = []
    statuses: list[DeliveryStatus] = []
    for entry in _as_dict(payload).get("entry") or []:
        for change in _as_dict(entry).get("changes") or []:
            change = _as_dict(change)
            if change.get("field") != "messages":
                continue
            value = _as_dict(change.get("value"))
            for raw in value.get("messages") or []:
                parsed = _parse_message(_as_dict(raw))
                if parsed is not None:
                    messages.append(parsed)
            for raw in value.get("statuses") or []:
                raw = _as_dict(raw)
                if not raw.get("id"):
                    continue
                errors = raw.get("errors") or [{}]
                first = _as_dict(errors[0]) if isinstance(errors, list) and errors else {}
                code = first.get("code")
                statuses.append(
                    DeliveryStatus(
                        message_id=str(raw["id"]),
                        status=str(raw.get("status") or ""),
                        recipient=phone_digits(str(raw.get("recipient_id") or "")),
                        error_code=int(code) if isinstance(code, (int, str)) and str(code).isdigit() else None,
                        error_title=str(first.get("title") or ""),
                    )
                )
    return messages, statuses

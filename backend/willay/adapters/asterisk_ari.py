"""Telephony through the Asterisk REST Interface (ARI), using the operator's OWN SIP trunk.

Flow of one call (`AsteriskAriAdapter.call`):

1. The number must be a valid E.164 number AND be on the allow-list (`WILLAY_CALL_ALLOWLIST`); otherwise
   `CallNotAllowed` is raised before any network activity. This is the demo safety net: nothing outside the
   list can ever be dialled, whatever the dispatch code asks for.
2. The ARI events WebSocket is opened first (so no event is lost), then `POST /ari/channels` originates the call
   to `PJSIP/<number>@<ASTERISK_TRUNK_NAME>` straight into the Stasis application (`ASTERISK_ARI_APP`).
3. `StasisStart` means the person answered: the tier recording is played with
   `POST /ari/channels/{id}/play` (media URI such as `sound:willay/evacuacion`).
4. `ChannelDtmfReceived`: key 1 = safe, key 2 = needs help (any other key is ignored). No key within
   `WILLAY_CALL_DTMF_TIMEOUT_S` (default 30 s), no answer, or a hang-up = NO_ANSWER.
5. The channel is always hung up (`DELETE /ari/channels/{id}`) and the event stream closed.

Credentials come from the environment only (ASTERISK_ARI_USER / ASTERISK_ARI_PASSWORD) and never appear in a
URL, a log line or a repr. See docs/TELEPHONY.md for the Asterisk side.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpx

from willay.domain.dispatch import ResponseKind, response_from_digit
from willay.domain.entities import AlertTier
from willay.ports.calls import CallError, CallNotAllowed, CallResult

ENV_URL = "ASTERISK_ARI_URL"
ENV_USER = "ASTERISK_ARI_USER"
ENV_PASSWORD = "ASTERISK_ARI_PASSWORD"
ENV_TRUNK = "ASTERISK_TRUNK_NAME"
ENV_APP = "ASTERISK_ARI_APP"
ENV_ALLOWLIST = "WILLAY_CALL_ALLOWLIST"
ENV_DTMF_TIMEOUT = "WILLAY_CALL_DTMF_TIMEOUT_S"
ENV_RING_TIMEOUT = "WILLAY_CALL_RING_TIMEOUT_S"
ENV_SOUND_PREFIX = "WILLAY_CALL_SOUND_"  # + PREPARATION | ATTENTION | EVACUATION

DEFAULT_SOUNDS = {
    AlertTier.PREPARATION: "sound:willay/preparacion",
    AlertTier.ATTENTION: "sound:willay/atencion",
    AlertTier.EVACUATION: "sound:willay/evacuacion",
}
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_TRUNK = re.compile(r"^[A-Za-z0-9_.-]+$")
_APP = re.compile(r"^[A-Za-z0-9_.-]+$")


class MissingAsteriskConfigError(RuntimeError):
    """Required Asterisk variables are missing (names only; values are never reported)."""


def normalize_number(number: str) -> str:
    return re.sub(r"[\s().-]", "", number or "")


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """Comma-separated E.164 numbers. Any malformed entry is an error (fail closed, never skip silently)."""
    numbers = set()
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        number = normalize_number(part)
        if not _E164.match(number):
            raise ValueError(f"{ENV_ALLOWLIST} contains a value that is not an E.164 number")
        numbers.add(number)
    return frozenset(numbers)


@dataclass(frozen=True)
class AsteriskAriConfig:
    base_url: str
    user: str
    password: str = field(repr=False)
    trunk_name: str
    allowlist: frozenset[str]
    app: str = "willay"
    sounds: Mapping[AlertTier, str] = field(default_factory=lambda: dict(DEFAULT_SOUNDS))
    ring_timeout_s: float = 30.0
    dtmf_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if not _TRUNK.match(self.trunk_name):
            raise ValueError(f"{ENV_TRUNK} may only contain letters, digits, '_', '.' and '-'")
        if not _APP.match(self.app):
            raise ValueError(f"{ENV_APP} may only contain letters, digits, '_', '.' and '-'")
        if self.ring_timeout_s <= 0 or self.dtmf_timeout_s <= 0:
            raise ValueError("call timeouts must be > 0")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "AsteriskAriConfig":
        missing = [n for n in (ENV_URL, ENV_USER, ENV_PASSWORD, ENV_TRUNK) if not environ.get(n)]
        if missing:
            raise MissingAsteriskConfigError("missing environment variables: " + ", ".join(missing))
        sounds = dict(DEFAULT_SOUNDS)
        for tier in (AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION):
            override = environ.get(ENV_SOUND_PREFIX + tier.value)
            if override:
                sounds[tier] = override
        return cls(
            base_url=environ[ENV_URL].rstrip("/"),
            user=environ[ENV_USER],
            password=environ[ENV_PASSWORD],
            trunk_name=environ[ENV_TRUNK],
            allowlist=parse_allowlist(environ.get(ENV_ALLOWLIST)),
            app=environ.get(ENV_APP) or "willay",
            sounds=sounds,
            ring_timeout_s=float(environ.get(ENV_RING_TIMEOUT) or 30.0),
            dtmf_timeout_s=float(environ.get(ENV_DTMF_TIMEOUT) or 30.0),
        )

    def basic_auth_header(self) -> str:
        return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()


class AriEventStream(Protocol):
    def recv(self, timeout: float) -> dict | None:
        """Next ARI event, or None when nothing arrived within `timeout` seconds."""

    def close(self) -> None: ...


class WebsocketEventStream:
    """ARI events over a WebSocket (needs the `websockets` package, imported lazily)."""

    def __init__(self, connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, config: AsteriskAriConfig) -> "WebsocketEventStream":
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise CallError("live calls need the 'websockets' package (pip install websockets)") from exc
        parts = urlsplit(config.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        url = f"{scheme}://{parts.netloc}/ari/events?app={quote(config.app)}"
        try:
            connection = connect(url, additional_headers={"Authorization": config.basic_auth_header()}, open_timeout=10)
        except Exception as exc:
            raise CallError(f"cannot open the ARI event stream: {type(exc).__name__}") from exc
        return cls(connection)

    def recv(self, timeout: float) -> dict | None:
        try:
            raw = self._connection.recv(timeout=max(timeout, 0.0))
        except TimeoutError:
            return None
        except Exception as exc:
            raise CallError(f"the ARI event stream failed: {type(exc).__name__}") from exc
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return event if isinstance(event, dict) else {}

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:  # closing is best effort
            pass


StreamFactory = Callable[[AsteriskAriConfig], AriEventStream]


def _seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


class AsteriskAriAdapter:
    def __init__(
        self,
        config: AsteriskAriConfig,
        client: httpx.Client | None = None,
        stream_factory: StreamFactory = WebsocketEventStream.open,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(base_url=config.base_url, timeout=10.0)
        self._stream_factory = stream_factory
        self._clock = clock

    def __repr__(self) -> str:
        c = self._config
        return f"AsteriskAriAdapter(base_url={c.base_url!r}, app={c.app!r}, trunk={c.trunk_name!r})"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "AsteriskAriAdapter":
        return cls(AsteriskAriConfig.from_env(environ))

    @property
    def allowlist(self) -> frozenset[str]:
        return self._config.allowlist

    # -- public port ----------------------------------------------------------------

    def call(self, number: str, tier: AlertTier) -> CallResult:
        number = normalize_number(number)
        if not _E164.match(number):
            raise CallNotAllowed("the number is not a valid E.164 number")
        if number not in self._config.allowlist:
            raise CallNotAllowed(f"the number is not on the allow-list ({ENV_ALLOWLIST})")
        media = self._config.sounds.get(tier)
        if not media:
            raise CallError(f"no prerecorded message is configured for tier {tier.value}")

        channel_id = f"willay-{uuid.uuid4().hex[:12]}"
        stream = self._stream_factory(self._config)  # opened BEFORE dialling: no event can be missed
        originated = False
        try:
            self._originate(number, channel_id, tier)
            originated = True
            return self._converse(stream, channel_id, media)
        finally:
            if originated:
                self._hang_up(channel_id)
            stream.close()

    # -- ARI REST -------------------------------------------------------------------

    def _request(self, method: str, path: str, **params: str) -> httpx.Response:
        try:
            response = self._client.request(
                method, path, params=params or None, auth=(self._config.user, self._config.password)
            )
        except httpx.HTTPError as exc:
            raise CallError(f"ARI request {method} {path.split('?')[0]} failed: {type(exc).__name__}") from exc
        return response

    def _originate(self, number: str, channel_id: str, tier: AlertTier) -> None:
        response = self._request(
            "POST",
            "/ari/channels",
            endpoint=f"PJSIP/{number}@{self._config.trunk_name}",
            app=self._config.app,
            appArgs=tier.value,
            channelId=channel_id,
            timeout=_seconds(self._config.ring_timeout_s),
        )
        if response.status_code >= 300:
            raise CallError(f"ARI refused to originate the call (HTTP {response.status_code})")

    def _play(self, channel_id: str, media: str) -> None:
        response = self._request("POST", f"/ari/channels/{channel_id}/play", media=media)
        if response.status_code >= 300:
            raise CallError(f"ARI refused to play the recording (HTTP {response.status_code})")

    def _hang_up(self, channel_id: str) -> None:
        try:
            self._request("DELETE", f"/ari/channels/{channel_id}")  # 404 = already gone, which is fine
        except CallError:
            pass  # best effort: the result of the call is already decided

    # -- event handling -------------------------------------------------------------

    @staticmethod
    def _ours(event: dict, channel_id: str) -> bool:
        return (event.get("channel") or {}).get("id") == channel_id

    def _converse(self, stream: AriEventStream, channel_id: str, media: str) -> CallResult:
        answered = self._wait_for_answer(stream, channel_id)
        if answered is not None:
            return answered
        self._play(channel_id, media)
        deadline = self._clock() + self._config.dtmf_timeout_s
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return CallResult(ResponseKind.NO_ANSWER, "no key was pressed in time")
            event = stream.recv(remaining)
            if event is None:
                return CallResult(ResponseKind.NO_ANSWER, "no key was pressed in time")
            if not self._ours(event, channel_id):
                continue
            kind = event.get("type")
            if kind == "ChannelDtmfReceived":
                outcome = response_from_digit(str(event.get("digit", "")))
                if outcome is not None:
                    return CallResult(outcome, f"key {event['digit']}")
            elif kind in ("ChannelDestroyed", "StasisEnd"):
                return CallResult(ResponseKind.NO_ANSWER, "the person hung up before pressing a key")

    def _wait_for_answer(self, stream: AriEventStream, channel_id: str) -> CallResult | None:
        """None when the call was answered (StasisStart); a NO_ANSWER result otherwise."""
        deadline = self._clock() + self._config.ring_timeout_s + 5.0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return CallResult(ResponseKind.NO_ANSWER, "nobody answered")
            event = stream.recv(remaining)
            if event is None:
                return CallResult(ResponseKind.NO_ANSWER, "nobody answered")
            if not self._ours(event, channel_id):
                continue
            kind = event.get("type")
            if kind == "StasisStart":
                return None
            if kind in ("ChannelDestroyed", "StasisEnd"):
                return CallResult(ResponseKind.NO_ANSWER, event.get("cause_txt") or "the call ended without an answer")

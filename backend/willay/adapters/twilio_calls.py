"""Optional telephony channel: automated voice calls through the Twilio REST API (disabled by default).

`TwilioCallAdapter` implements `CallPort`. One call is created with inline TwiML (`<Gather numDigits="1">` plus a Spanish
`<Say language="es-MX">` message for the tier). The key press arrives at the webhook `POST /telephony/twilio/gather`,
whose `X-Twilio-Signature` is validated, and is handed to the waiting `call()` (1 = SAFE, 2 = NEEDS_HELP). A call that
ends without a key press, or times out, is NO_ANSWER. Numbers outside `WILLAY_CALL_ALLOWLIST` are refused before any
request. Credentials come from the environment only and never appear in a URL, log line, error or `repr`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping
from xml.sax.saxutils import escape

import httpx

from willay.adapters.asterisk_ari import ENV_ALLOWLIST, ENV_DTMF_TIMEOUT, ENV_RING_TIMEOUT, normalize_number, parse_allowlist
from willay.domain.dispatch import ResponseKind, response_from_digit
from willay.domain.entities import AlertTier
from willay.ports.calls import CallError, CallNotAllowed, CallResult

ENV_SID = "TWILIO_ACCOUNT_SID"
ENV_TOKEN = "TWILIO_AUTH_TOKEN"
ENV_FROM = "TWILIO_FROM_NUMBER"
ENV_PUBLIC_URL = "WILLAY_PUBLIC_BASE_URL"
API_ROOT = "https://api.twilio.com/2010-04-01"
GATHER_PATH = "/telephony/twilio/gather"
TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
SAY = 'language="es-MX"'

_FOOTER = "Esta alerta complementa a SENAMHI, INDECI y ENFEN."
_KEYS = "Presione 1 si está a salvo. Presione 2 si necesita ayuda."
SPOKEN: Mapping[AlertTier, str] = {
    AlertTier.PREPARATION: f"Alerta de Willay Relaves. Prepárese: pueden venir lluvias fuertes y hay riesgo en relaves río arriba. {_FOOTER} {_KEYS}",
    AlertTier.ATTENTION: f"Atención. Se esperan lluvias fuertes y hay riesgo de huaico en las próximas horas. Revise su punto seguro. {_FOOTER} {_KEYS}",
    AlertTier.EVACUATION: f"Evacúe ahora hacia su punto seguro. Hay peligro por el relave río arriba. {_FOOTER} {_KEYS}",
}
NO_KEY_TEXT = "No recibimos su respuesta. Si necesita ayuda, comuníquese con su municipalidad."
THANKS_TEXT = "Gracias. Su respuesta fue registrada."
RETRY_TEXT = "Opción no válida. " + _KEYS


class MissingTwilioConfigError(RuntimeError):
    """Required Twilio variables are missing (names only; values are never reported)."""


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted(key + value of every POST parameter)))."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()).decode()


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str = field(repr=False)
    auth_token: str = field(repr=False)
    from_number: str
    public_base_url: str
    allowlist: frozenset[str]
    ring_timeout_s: float = 30.0
    dtmf_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if not _SID.match(self.account_sid):
            raise ValueError(f"{ENV_SID} is not a valid Twilio account SID")
        if not _E164.match(self.from_number):
            raise ValueError(f"{ENV_FROM} must be an E.164 number")
        if not self.public_base_url.startswith("https://"):
            raise ValueError(f"{ENV_PUBLIC_URL} must be an https:// URL (for example your Cloudflare Tunnel address)")
        if self.ring_timeout_s <= 0 or self.dtmf_timeout_s <= 0:
            raise ValueError("call timeouts must be > 0")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TwilioConfig":
        missing = [n for n in (ENV_SID, ENV_TOKEN, ENV_FROM, ENV_PUBLIC_URL) if not environ.get(n)]
        if missing:
            raise MissingTwilioConfigError("missing environment variables: " + ", ".join(missing))
        return cls(
            account_sid=environ[ENV_SID].strip(),
            auth_token=environ[ENV_TOKEN].strip(),
            from_number=normalize_number(environ[ENV_FROM]),
            public_base_url=environ[ENV_PUBLIC_URL].strip().rstrip("/"),
            allowlist=parse_allowlist(environ.get(ENV_ALLOWLIST)),
            ring_timeout_s=float(environ.get(ENV_RING_TIMEOUT) or 30.0),
            dtmf_timeout_s=float(environ.get(ENV_DTMF_TIMEOUT) or 30.0),
        )


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    outcome: ResponseKind | None = None


def gather_twiml(action_url: str, text: str, timeout_s: float) -> str:
    return (
        "<Response>"
        f'<Gather numDigits="1" action="{escape(action_url, {chr(34): "&quot;"})}" method="POST" timeout="{int(timeout_s)}">'
        f"<Say {SAY}>{escape(text)}</Say></Gather>"
        f"<Say {SAY}>{escape(NO_KEY_TEXT)}</Say>"
        "</Response>"
    )


def hangup_twiml(text: str = THANKS_TEXT) -> str:
    return f"<Response><Say {SAY}>{escape(text)}</Say><Hangup/></Response>"


class TwilioCallAdapter:
    def __init__(
        self,
        config: TwilioConfig,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=15.0)
        self._clock = clock
        self._poll = poll_interval_s
        self._waiters: dict[str, _Waiter] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TwilioCallAdapter(from={self._config.from_number!r}, public_base_url={self._config.public_base_url!r})"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "TwilioCallAdapter":
        return cls(TwilioConfig.from_env(environ))

    @property
    def allowlist(self) -> frozenset[str]:
        return self._config.allowlist

    # --- webhook side -------------------------------------------------------------------------------------

    def public_url(self, path: str, query: str = "") -> str:
        return self._config.public_base_url + path + (f"?{query}" if query else "")

    def validate_signature(self, url: str, params: Mapping[str, str], signature: str | None) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(compute_signature(self._config.auth_token, url, params), signature)

    def is_waiting(self, call_sid: str) -> bool:
        with self._lock:
            return call_sid in self._waiters

    def receive_digits(self, call_sid: str, digits: str) -> str:
        """Hand a key press to the waiting call. Returns 'accepted', 'ignored' (not 1 or 2) or 'unknown' (no such call)."""
        with self._lock:
            waiter = self._waiters.get(call_sid)
        if waiter is None:
            return "unknown"
        response = response_from_digit((digits or "").strip()[:1])
        if response is None:
            return "ignored"
        waiter.outcome = response
        waiter.event.set()
        return "accepted"

    def webhook_twiml(self, status: str) -> str:
        if status == "ignored":
            return gather_twiml(self.public_url(GATHER_PATH), RETRY_TEXT, self._config.dtmf_timeout_s)
        return hangup_twiml()

    # --- REST -------------------------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raw = f"{self._config.account_sid}:{self._config.auth_token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._client.request(method, f"{API_ROOT}/Accounts/{self._config.account_sid}{path}", headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise CallError(f"the Twilio request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise CallError(f"Twilio rejected the request (HTTP {response.status_code})")
        try:
            body = response.json()
        except ValueError as exc:
            raise CallError("Twilio returned a non-JSON body") from exc
        return body if isinstance(body, dict) else {}

    def _hang_up(self, call_sid: str) -> None:
        try:
            self._request("POST", f"/Calls/{call_sid}.json", data={"Status": "completed"})
        except CallError:
            pass  # best effort: the call may already be over

    # --- public port --------------------------------------------------------------------------------------------

    def call(self, number: str, tier: AlertTier) -> CallResult:
        number = normalize_number(number)
        if not _E164.match(number):
            raise CallNotAllowed("the number is not a valid E.164 number")
        if number not in self._config.allowlist:
            raise CallNotAllowed(f"the number is not on the allow-list ({ENV_ALLOWLIST})")
        text = SPOKEN.get(tier)
        if text is None:
            raise CallError(f"no spoken message is configured for tier {tier.value}")

        twiml = gather_twiml(self.public_url(GATHER_PATH), text, self._config.dtmf_timeout_s)
        created = self._request(
            "POST",
            "/Calls.json",
            data={"To": number, "From": self._config.from_number, "Twiml": twiml, "Timeout": str(int(self._config.ring_timeout_s))},
        )
        call_sid = str(created.get("sid") or "")
        if not call_sid:
            raise CallError("Twilio did not return a call SID")
        waiter = _Waiter()
        with self._lock:
            self._waiters[call_sid] = waiter
        ended = False
        try:
            deadline = self._clock() + self._config.ring_timeout_s + self._config.dtmf_timeout_s
            while True:
                if waiter.event.wait(max(self._poll, 0.001)) and waiter.outcome is not None:
                    return CallResult(waiter.outcome, f"twilio: key pressed ({call_sid[:6]}...)")
                status = str(self._request("GET", f"/Calls/{call_sid}.json").get("status", ""))
                if status in TERMINAL_STATUSES:
                    ended = True
                    if waiter.outcome is not None:
                        return CallResult(waiter.outcome, "twilio: key pressed")
                    return CallResult(ResponseKind.NO_ANSWER, f"twilio: call ended ({status}) without a key press")
                if self._clock() >= deadline:
                    return CallResult(ResponseKind.NO_ANSWER, "twilio: no key press before the timeout")
        finally:
            with self._lock:
                self._waiters.pop(call_sid, None)
            if not ended:
                self._hang_up(call_sid)

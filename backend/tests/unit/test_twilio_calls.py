"""Twilio call adapter and its webhook, fully mocked: no request leaves the machine and no call is placed."""

import base64
import hashlib
import hmac
import json
import threading
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from willay.adapters.twilio_calls import (
    MissingTwilioConfigError,
    TwilioCallAdapter,
    TwilioConfig,
    compute_signature,
)
from willay.api.app import create_app
from willay.api.dependencies import attach_calls_from_env, synthetic_dependencies
from willay.domain.dispatch import ResponseKind
from willay.domain.entities import AlertTier
from willay.ports.calls import CallError, CallNotAllowed

SID, TOKEN, FROM = "ACtest0000000000000000000000000000", "token-not-real", "+15005550006"
BASE = "https://willay.example.test"
ALLOWED = "+51900000001"
ENV = {
    "WILLAY_CALLS_ENABLED": "1",
    "WILLAY_CALLS_PROVIDER": "twilio",
    "TWILIO_ACCOUNT_SID": SID,
    "TWILIO_AUTH_TOKEN": TOKEN,
    "TWILIO_FROM_NUMBER": FROM,
    "WILLAY_PUBLIC_BASE_URL": BASE,
    "WILLAY_CALL_ALLOWLIST": ALLOWED,
}


class FakeTwilio:
    """Records REST requests; the call status sequence is scripted."""

    def __init__(self, statuses=("ringing", "in-progress", "completed"), create_status=201):
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses)
        self.create_status = create_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/Calls.json"):
            if self.create_status >= 400:
                return httpx.Response(self.create_status, json={"message": f"bad credentials {TOKEN}"})
            return httpx.Response(201, json={"sid": "CA123", "status": "queued"})
        if request.method == "GET":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"sid": "CA123", "status": status})
        return httpx.Response(200, json={"sid": "CA123", "status": "completed"})  # hang-up / update


def make(fake=None, **kwargs):
    fake = fake or FakeTwilio()
    config = TwilioConfig.from_env(ENV)
    client = httpx.Client(transport=httpx.MockTransport(fake))
    adapter = TwilioCallAdapter(config, client=client, sleep=lambda s: None, poll_interval_s=0.0, **kwargs)
    return adapter, fake


def posted_form(fake) -> dict:
    request = next(r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/Calls.json"))
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --- configuration -----------------------------------------------------------------------------------


def test_config_reports_missing_variable_names_never_values():
    with pytest.raises(MissingTwilioConfigError) as error:
        TwilioConfig.from_env({"TWILIO_ACCOUNT_SID": SID})
    message = str(error.value)
    assert "TWILIO_AUTH_TOKEN" in message and "TWILIO_FROM_NUMBER" in message and "WILLAY_PUBLIC_BASE_URL" in message
    assert SID not in message


def test_public_base_url_must_be_https_and_numbers_must_be_e164():
    with pytest.raises(ValueError):
        TwilioConfig.from_env({**ENV, "WILLAY_PUBLIC_BASE_URL": "http://willay.example.test"})
    with pytest.raises(ValueError):
        TwilioConfig.from_env({**ENV, "TWILIO_FROM_NUMBER": "5005550006"})


def test_repr_never_shows_the_credentials():
    adapter, _ = make()
    assert TOKEN not in repr(adapter) and TOKEN not in repr(adapter._config)  # noqa: SLF001


# --- allow-list ------------------------------------------------------------------------------------------


def test_numbers_off_the_allow_list_are_refused_before_any_request():
    adapter, fake = make()
    with pytest.raises(CallNotAllowed):
        adapter.call("+51999999999", AlertTier.ATTENTION)
    with pytest.raises(CallNotAllowed):
        adapter.call("not-a-number", AlertTier.ATTENTION)
    assert fake.requests == []


def test_an_empty_allow_list_refuses_everything():
    config = TwilioConfig.from_env({**ENV, "WILLAY_CALL_ALLOWLIST": ""})
    adapter = TwilioCallAdapter(config, client=httpx.Client(transport=httpx.MockTransport(FakeTwilio())))
    with pytest.raises(CallNotAllowed):
        adapter.call(ALLOWED, AlertTier.ATTENTION)


# --- REST request --------------------------------------------------------------------------------------------


def test_call_posts_inline_twiml_with_a_gather_and_spanish_speech():
    adapter, fake = make()
    adapter.call(ALLOWED, AlertTier.ATTENTION)
    request = next(r for r in fake.requests if r.method == "POST")
    assert str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()
    form = posted_form(fake)
    assert form["To"] == ALLOWED and form["From"] == FROM
    twiml = form["Twiml"]
    assert '<Gather numDigits="1"' in twiml and f'action="{BASE}/telephony/twilio/gather' in twiml
    assert '<Say language="es-MX"' in twiml and "Presione 1" in twiml and "SENAMHI" in twiml
    assert TOKEN not in request.content.decode()


def test_each_tier_speaks_its_own_message():
    texts = {}
    for tier in (AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION):
        adapter, fake = make()
        adapter.call(ALLOWED, tier)
        texts[tier] = posted_form(fake)["Twiml"]
    assert len(set(texts.values())) == 3
    assert "Evacúe" in texts[AlertTier.EVACUATION]


def test_rest_errors_become_call_errors_without_leaking_the_token():
    adapter, _ = make(FakeTwilio(create_status=401))
    with pytest.raises(CallError) as error:
        adapter.call(ALLOWED, AlertTier.ATTENTION)
    assert TOKEN not in str(error.value) and "401" in str(error.value)


# --- outcomes ---------------------------------------------------------------------------------------------------


def run_call(adapter, digits=None, delay_until_waiting=True):
    """Place the call in a thread and, once it waits, deliver the webhook digits like Twilio would."""
    result = {}

    def target():
        result["value"] = adapter.call(ALLOWED, AlertTier.ATTENTION)

    thread = threading.Thread(target=target)
    thread.start()
    if digits is not None:
        for _ in range(500):
            if adapter.is_waiting("CA123"):
                break
            threading.Event().wait(0.01)
        adapter.receive_digits("CA123", digits)
    thread.join(timeout=10)
    assert not thread.is_alive()
    return result["value"]


def test_key_1_is_safe_and_key_2_needs_help():
    adapter, _ = make(FakeTwilio(statuses=("in-progress",)))
    assert run_call(adapter, "1").outcome is ResponseKind.SAFE
    adapter, _ = make(FakeTwilio(statuses=("in-progress",)))
    assert run_call(adapter, "2").outcome is ResponseKind.NEEDS_HELP


def test_other_keys_are_ignored_until_the_call_ends():
    adapter, _ = make(FakeTwilio(statuses=("in-progress", "in-progress", "completed")))
    assert run_call(adapter, "7").outcome is ResponseKind.NO_ANSWER


@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled", "completed"])
def test_a_call_that_ends_without_a_key_press_is_no_answer(status):
    adapter, _ = make(FakeTwilio(statuses=(status,)))
    assert adapter.call(ALLOWED, AlertTier.ATTENTION).outcome is ResponseKind.NO_ANSWER


def test_a_timeout_hangs_the_call_up_and_counts_as_no_answer():
    clock = iter(range(0, 1000, 20))
    config = TwilioConfig.from_env({**ENV, "WILLAY_CALL_DTMF_TIMEOUT_S": "30"})
    fake = FakeTwilio(statuses=("in-progress",))
    adapter = TwilioCallAdapter(
        config, client=httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None, clock=lambda: float(next(clock)), poll_interval_s=0.0
    )
    assert adapter.call(ALLOWED, AlertTier.ATTENTION).outcome is ResponseKind.NO_ANSWER
    assert any(r.method == "POST" and r.url.path.endswith("/Calls/CA123.json") for r in fake.requests)


# --- webhook signature + route ---------------------------------------------------------------------------------------


def test_signature_matches_the_documented_algorithm():
    url = "https://mycompany.example/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1", "Digits": "1", "From": "+14158675309"}
    payload = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(b"12345", payload.encode(), hashlib.sha1).digest()).decode()
    assert compute_signature("12345", url, params) == expected


def test_validate_rejects_tampered_params_urls_and_missing_signatures():
    adapter, _ = make()
    url, params = f"{BASE}/telephony/twilio/gather", {"CallSid": "CA123", "Digits": "2"}
    good = compute_signature(TOKEN, url, params)
    assert adapter.validate_signature(url, params, good)
    assert not adapter.validate_signature(url, {**params, "Digits": "1"}, good)
    assert not adapter.validate_signature(url + "?x=1", params, good)
    assert not adapter.validate_signature(url, params, None)
    assert not adapter.validate_signature(url, params, "")


def twilio_client(waiting_digits=None):
    deps = attach_calls_from_env(synthetic_dependencies(), ENV)
    assert isinstance(deps.calls, TwilioCallAdapter)
    return TestClient(create_app(deps)), deps


def signed(path, params):
    return {"X-Twilio-Signature": compute_signature(TOKEN, BASE + path, params)}


def test_gather_webhook_delivers_the_digit_and_answers_with_twiml():
    client, deps = twilio_client()
    path, params = "/telephony/twilio/gather", {"CallSid": "CA123", "Digits": "2"}
    response = client.post(path, data=params, headers=signed(path, params))
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/xml")
    assert "<Response>" in response.text and "Hangup" in response.text


def test_gather_webhook_rejects_a_bad_signature_without_delivering_anything():
    client, deps = twilio_client()
    received = []
    deps.calls.receive_digits = lambda sid, digits: received.append((sid, digits))  # type: ignore[method-assign]
    response = client.post("/telephony/twilio/gather", data={"CallSid": "CA123", "Digits": "1"}, headers={"X-Twilio-Signature": "forged"})
    assert response.status_code == 403 and received == []
    assert client.post("/telephony/twilio/gather", data={"CallSid": "CA123", "Digits": "1"}).status_code == 403


def test_gather_webhook_is_absent_unless_twilio_is_the_provider():
    deps = attach_calls_from_env(synthetic_dependencies(), {})
    client = TestClient(create_app(deps))
    assert client.post("/telephony/twilio/gather", data={"CallSid": "x", "Digits": "1"}).status_code == 404


# --- provider selection --------------------------------------------------------------------------------------------------


def test_provider_selection_is_explicit_and_safe_by_default():
    base = synthetic_dependencies
    assert attach_calls_from_env(base(), {**ENV, "WILLAY_CALLS_ENABLED": "0"}).calls_mode == "simulated"
    twilio = attach_calls_from_env(base(), ENV)
    assert twilio.calls_mode.startswith("twilio") and "1 number" in twilio.calls_mode
    assert attach_calls_from_env(base(), {**ENV, "WILLAY_CALLS_PROVIDER": "simulated"}).calls_mode == "simulated"
    missing = attach_calls_from_env(base(), {"WILLAY_CALLS_ENABLED": "1", "WILLAY_CALLS_PROVIDER": "twilio"})
    assert missing.calls_mode.startswith("simulated") and "TWILIO_ACCOUNT_SID" in missing.calls_mode
    unknown = attach_calls_from_env(base(), {"WILLAY_CALLS_ENABLED": "1", "WILLAY_CALLS_PROVIDER": "carrier-pigeon"})
    assert unknown.calls_mode.startswith("simulated") and "carrier-pigeon" in unknown.calls_mode

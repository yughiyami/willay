"""Asterisk ARI call adapter: mocked HTTP transport and a fake event stream. No network, no real call."""

import base64
import json
import sys
import types

import httpx
import pytest

from willay.adapters.asterisk_ari import (
    AsteriskAriAdapter,
    AsteriskAriConfig,
    MissingAsteriskConfigError,
    WebsocketEventStream,
    parse_allowlist,
)
from willay.adapters.simulated_call import SimulatedCallAdapter
from willay.domain.dispatch import ResponseKind, response_from_digit
from willay.domain.entities import AlertTier
from willay.ports.calls import CallError, CallNotAllowed

ALLOWED = "+51999000111"
BLOCKED = "+51999000222"
BASE = "http://asterisk.test:8088"


def config(**changes) -> AsteriskAriConfig:
    base = dict(
        base_url=BASE,
        user="willay-ari",
        password="not-a-real-password",
        trunk_name="my-trunk",
        allowlist=frozenset({ALLOWED}),
        dtmf_timeout_s=5.0,
        ring_timeout_s=5.0,
    )
    base.update(changes)
    return AsteriskAriConfig(**base)


class FakeStream:
    """Replays scripted ARI events. `None` in the script means "nothing arrived before the timeout"."""

    def __init__(self, script, log):
        self.script, self.log, self.closed, self.timeouts = list(script), log, False, []

    def recv(self, timeout):
        self.timeouts.append(timeout)
        return self.script.pop(0) if self.script else None

    def close(self):
        self.closed = True
        self.log.append("stream.close")


def channel_event(kind, channel_id, **extra):
    return {"type": kind, "channel": {"id": channel_id}, **extra}


class Harness:
    """Wires the adapter to a mocked HTTP transport and a stream whose script may depend on the channel id."""

    def __init__(self, script, cfg=None, http_status=None):
        self.requests: list[httpx.Request] = []
        self.log: list[str] = []
        self.script, self.http_status = script, http_status or {}
        self.stream: FakeStream | None = None
        self.streams_opened = 0
        client = httpx.Client(transport=httpx.MockTransport(self._handle), base_url=BASE)
        self.adapter = AsteriskAriAdapter(cfg or config(), client, stream_factory=self._open_stream)

    def _open_stream(self, cfg):
        self.log.append("stream.open")
        self.streams_opened += 1
        self.stream = FakeStream([], self.log)
        return self.stream

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.log.append(f"{request.method} {request.url.path}")
        last = request.url.path.split("/")[-1]
        status = self.http_status.get((request.method, last)) or self.http_status.get((request.method, "*"))
        if request.method == "POST" and request.url.path == "/ari/channels":
            channel_id = request.url.params["channelId"]
            self.stream.script = self.script(channel_id)
            if status:
                return httpx.Response(status, json={"message": "nope"})
            return httpx.Response(200, json={"id": channel_id, "state": "Down"})
        if status:
            return httpx.Response(status, json={"message": "nope"})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "pb-1", "state": "queued"})
        return httpx.Response(204)

    def call(self, number=ALLOWED, tier=AlertTier.EVACUATION):
        return self.adapter.call(number, tier)

    def channel_id(self):
        return next(r.url.params["channelId"] for r in self.requests if r.url.path == "/ari/channels")


def script_for(*steps):
    """steps: ("StasisStart",) | ("dtmf", "1") | ("Destroyed",) | None | ("other", ...). Resolved per channel id."""

    def build(channel_id):
        events = []
        for step in steps:
            if step is None:
                events.append(None)
            elif step[0] == "StasisStart":
                events.append(channel_event("StasisStart", channel_id))
            elif step[0] == "dtmf":
                events.append(channel_event("ChannelDtmfReceived", channel_id, digit=step[1]))
            elif step[0] == "Destroyed":
                events.append(channel_event("ChannelDestroyed", channel_id, cause_txt="No Answer"))
            elif step[0] == "stranger":
                events.append(channel_event("ChannelDtmfReceived", "someone-else", digit="2"))
        return events

    return build


# --- safety: the allow-list ----------------------------------------------------------


def test_a_number_outside_the_allowlist_is_refused_without_any_network_activity():
    harness = Harness(script_for())
    with pytest.raises(CallNotAllowed):
        harness.call(BLOCKED)
    assert harness.requests == [] and harness.streams_opened == 0


def test_an_empty_allowlist_refuses_every_number():
    harness = Harness(script_for(), cfg=config(allowlist=frozenset()))
    with pytest.raises(CallNotAllowed):
        harness.call(ALLOWED)
    assert harness.requests == []


@pytest.mark.parametrize("bad", ["999000111", "+0123456", "+51 999 000 111; DROP", "", "tel:+51999000111"])
def test_malformed_numbers_are_refused_even_if_someone_allowlisted_them(bad):
    harness = Harness(script_for(), cfg=config(allowlist=frozenset({bad})))
    with pytest.raises(CallNotAllowed):
        harness.call(bad)
    assert harness.requests == []


def test_spaces_in_the_number_do_not_bypass_or_break_the_allowlist():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")))
    assert harness.call("+51 999 000 111").outcome is ResponseKind.SAFE


def test_parse_allowlist_normalises_and_rejects_garbage():
    assert parse_allowlist("+51999000111, +51 999 000 222 ,") == frozenset({"+51999000111", "+51999000222"})
    assert parse_allowlist("") == frozenset() and parse_allowlist(None) == frozenset()
    with pytest.raises(ValueError):
        parse_allowlist("+51999000111,not-a-number")


# --- the call flow ------------------------------------------------------------------


def test_originate_builds_the_pjsip_endpoint_from_the_trunk_and_enters_the_stasis_app():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")))
    harness.call()
    originate = next(r for r in harness.requests if r.method == "POST" and r.url.path == "/ari/channels")
    params = originate.url.params
    assert params["endpoint"] == f"PJSIP/{ALLOWED}@my-trunk"
    assert params["app"] == "willay"
    assert params["channelId"].startswith("willay-")
    assert params["timeout"] == "5"
    expected = "Basic " + base64.b64encode(b"willay-ari:not-a-real-password").decode()
    assert originate.headers["authorization"] == expected


def test_the_event_stream_is_opened_before_dialling_so_no_event_is_lost():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")))
    harness.call()
    assert harness.log.index("stream.open") < harness.log.index("POST /ari/channels")


def test_key_1_means_safe_and_the_tier_recording_is_played_to_the_answered_channel():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")))
    result = harness.call(tier=AlertTier.EVACUATION)
    assert result.outcome is ResponseKind.SAFE
    play = next(r for r in harness.requests if r.url.path.endswith("/play"))
    assert play.url.path == f"/ari/channels/{harness.channel_id()}/play"
    assert play.url.params["media"] == "sound:willay/evacuacion"


def test_each_tier_plays_its_own_recording():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")))
    harness.call(tier=AlertTier.ATTENTION)
    play = next(r for r in harness.requests if r.url.path.endswith("/play"))
    assert play.url.params["media"] == "sound:willay/atencion"


def test_recording_paths_are_configurable():
    cfg = config(sounds={AlertTier.EVACUATION: "sound:/var/lib/asterisk/sounds/custom/evac"})
    harness = Harness(script_for(("StasisStart",), ("dtmf", "2")), cfg=cfg)
    result = harness.call(tier=AlertTier.EVACUATION)
    play = next(r for r in harness.requests if r.url.path.endswith("/play"))
    assert play.url.params["media"] == "sound:/var/lib/asterisk/sounds/custom/evac"
    assert result.outcome is ResponseKind.NEEDS_HELP


def test_key_2_means_needs_help():
    assert Harness(script_for(("StasisStart",), ("dtmf", "2"))).call().outcome is ResponseKind.NEEDS_HELP


def test_other_keys_and_other_channels_are_ignored():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "7"), ("stranger",), ("dtmf", "1")))
    assert harness.call().outcome is ResponseKind.SAFE


def test_no_key_press_within_the_timeout_is_no_answer_and_the_call_is_hung_up():
    harness = Harness(script_for(("StasisStart",), None))
    result = harness.call()
    assert result.outcome is ResponseKind.NO_ANSWER
    assert harness.log[-2:] == [f"DELETE /ari/channels/{harness.channel_id()}", "stream.close"]


def test_the_dtmf_wait_uses_the_configured_timeout():
    harness = Harness(script_for(("StasisStart",), None), cfg=config(dtmf_timeout_s=30.0))
    harness.call()
    assert harness.stream.timeouts[-1] == pytest.approx(30.0, abs=1.0)


def test_default_dtmf_timeout_is_30_seconds():
    assert AsteriskAriConfig(BASE, "u", "p", "t", frozenset()).dtmf_timeout_s == 30.0


def test_a_call_nobody_answers_is_no_answer_and_nothing_is_played():
    harness = Harness(script_for(("Destroyed",)))
    result = harness.call()
    assert result.outcome is ResponseKind.NO_ANSWER
    assert not any(r.url.path.endswith("/play") for r in harness.requests)


def test_a_ring_timeout_is_no_answer():
    assert Harness(script_for(None)).call().outcome is ResponseKind.NO_ANSWER


def test_hanging_up_before_pressing_a_key_is_no_answer():
    harness = Harness(script_for(("StasisStart",), ("Destroyed",)))
    assert harness.call().outcome is ResponseKind.NO_ANSWER


def test_hangup_is_always_sent_and_the_stream_closed_after_an_answer():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")))
    harness.call()
    assert f"DELETE /ari/channels/{harness.channel_id()}" in harness.log
    assert harness.stream.closed is True


def test_a_rejected_originate_is_a_call_error_and_closes_the_stream():
    harness = Harness(script_for(), http_status={("POST", "channels"): 500})
    with pytest.raises(CallError):
        harness.call()
    assert harness.stream.closed is True
    assert not any(r.method == "DELETE" for r in harness.requests)


def test_a_failed_playback_is_a_call_error_but_the_call_is_still_hung_up():
    harness = Harness(script_for(("StasisStart",)), http_status={("POST", "play"): 500})
    with pytest.raises(CallError):
        harness.call()
    assert any(r.method == "DELETE" for r in harness.requests)


def test_a_hangup_of_an_already_gone_channel_is_not_an_error():
    harness = Harness(script_for(("StasisStart",), ("dtmf", "1")), http_status={("DELETE", "*"): 404})
    assert harness.call().outcome is ResponseKind.SAFE


def test_a_tier_without_a_recording_is_refused_before_any_network_activity():
    harness = Harness(script_for(), cfg=config(sounds={AlertTier.EVACUATION: "sound:willay/evacuacion"}))
    with pytest.raises(CallError):
        harness.call(tier=AlertTier.PENDING_CONFIRMATION)
    assert harness.requests == [] and harness.streams_opened == 0


def test_the_password_never_appears_in_the_adapter_or_config_repr():
    harness = Harness(script_for())
    assert "not-a-real-password" not in repr(harness.adapter) + repr(config())


# --- configuration from the environment ----------------------------------------------

FULL_ENV = {
    "ASTERISK_ARI_URL": BASE,
    "ASTERISK_ARI_USER": "willay-ari",
    "ASTERISK_ARI_PASSWORD": "secret-from-env",
    "ASTERISK_TRUNK_NAME": "my-trunk",
    "WILLAY_CALL_ALLOWLIST": "+51999000111",
}


def test_config_from_env_applies_the_documented_defaults():
    cfg = AsteriskAriConfig.from_env(FULL_ENV)
    assert (cfg.app, cfg.dtmf_timeout_s, cfg.ring_timeout_s) == ("willay", 30.0, 30.0)
    assert cfg.allowlist == frozenset({"+51999000111"})
    assert cfg.sounds[AlertTier.EVACUATION] == "sound:willay/evacuacion"
    assert cfg.trunk_name == "my-trunk"


def test_config_from_env_reads_overrides():
    env = {
        **FULL_ENV,
        "ASTERISK_ARI_APP": "other",
        "WILLAY_CALL_DTMF_TIMEOUT_S": "12",
        "WILLAY_CALL_SOUND_EVACUATION": "sound:custom/evac",
    }
    cfg = AsteriskAriConfig.from_env(env)
    assert cfg.app == "other" and cfg.dtmf_timeout_s == 12.0
    assert cfg.sounds[AlertTier.EVACUATION] == "sound:custom/evac"


def test_config_from_env_names_every_missing_variable_without_values():
    with pytest.raises(MissingAsteriskConfigError) as error:
        AsteriskAriConfig.from_env({"ASTERISK_ARI_USER": "x"})
    message = str(error.value)
    for name in ("ASTERISK_ARI_URL", "ASTERISK_ARI_PASSWORD", "ASTERISK_TRUNK_NAME"):
        assert name in message


def test_an_unsafe_trunk_name_is_rejected():
    with pytest.raises(ValueError):
        AsteriskAriConfig.from_env({**FULL_ENV, "ASTERISK_TRUNK_NAME": "trunk&evil=1"})


def test_from_env_returns_a_working_adapter():
    adapter = AsteriskAriAdapter.from_env(FULL_ENV)
    assert isinstance(adapter, AsteriskAriAdapter)
    assert "secret-from-env" not in repr(adapter)


# --- the WebSocket wrapper (a fake `websockets` module, no network) --------------------


class FakeConnection:
    def __init__(self, messages):
        self.messages, self.closed, self.recv_timeouts = list(messages), False, []

    def recv(self, timeout=None):
        self.recv_timeouts.append(timeout)
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_websockets(monkeypatch):
    calls = {}

    def connect(url, **kwargs):
        calls["url"], calls["kwargs"] = url, kwargs
        calls["connection"] = FakeConnection(calls.get("messages", []))
        return calls["connection"]

    sync_client = types.ModuleType("websockets.sync.client")
    sync_client.connect = connect
    sync = types.ModuleType("websockets.sync")
    sync.client = sync_client
    root = types.ModuleType("websockets")
    root.sync = sync
    monkeypatch.setitem(sys.modules, "websockets", root)
    monkeypatch.setitem(sys.modules, "websockets.sync", sync)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", sync_client)
    return calls


def test_websocket_stream_connects_to_the_events_endpoint_with_basic_auth_and_no_credentials_in_the_url(fake_websockets):
    fake_websockets["messages"] = [json.dumps({"type": "StasisStart", "channel": {"id": "c1"}})]
    stream = WebsocketEventStream.open(config())
    assert fake_websockets["url"] == "ws://asterisk.test:8088/ari/events?app=willay"
    assert "not-a-real-password" not in fake_websockets["url"]
    expected = "Basic " + base64.b64encode(b"willay-ari:not-a-real-password").decode()
    assert fake_websockets["kwargs"]["additional_headers"]["Authorization"] == expected
    assert stream.recv(1.0) == {"type": "StasisStart", "channel": {"id": "c1"}}
    assert stream.recv(0.5) is None  # TimeoutError becomes None
    stream.close()
    assert fake_websockets["connection"].closed is True


def test_https_urls_use_wss(fake_websockets):
    WebsocketEventStream.open(config(base_url="https://asterisk.test:8089"))
    assert fake_websockets["url"].startswith("wss://asterisk.test:8089/ari/events")


def test_a_missing_websockets_package_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)
    monkeypatch.setitem(sys.modules, "websockets.sync", None)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", None)
    with pytest.raises(CallError, match="websockets"):
        WebsocketEventStream.open(config())


# --- the simulated adapter and the keypad ----------------------------------------------


def test_simulated_calls_return_scripted_outcomes_and_record_the_attempt():
    sim = SimulatedCallAdapter({"+51900000001": ResponseKind.SAFE, "+51900000002": ResponseKind.NEEDS_HELP})
    assert sim.call("+51900000001", AlertTier.ATTENTION).outcome is ResponseKind.SAFE
    assert sim.call("+51900000002", AlertTier.EVACUATION).outcome is ResponseKind.NEEDS_HELP
    assert sim.call("+51900000009", AlertTier.EVACUATION).outcome is ResponseKind.NO_ANSWER
    assert sim.calls == [
        ("+51900000001", AlertTier.ATTENTION),
        ("+51900000002", AlertTier.EVACUATION),
        ("+51900000009", AlertTier.EVACUATION),
    ]
    assert "simulated" in sim.call("+51900000001", AlertTier.ATTENTION).detail


def test_keypad_mapping():
    assert response_from_digit("1") is ResponseKind.SAFE
    assert response_from_digit("2") is ResponseKind.NEEDS_HELP
    assert response_from_digit("5") is None

"""GET /signals, the live refresh (mocked search and model), the simulated set, and the guardrails over HTTP."""

import dataclasses
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from willay.adapters.in_memory import InMemorySiteRepository
from willay.adapters.signals_fixture import load_simulated_signals
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies
from willay.application.public_signals import PublicSignalsHub
from willay.domain.signals import SIGNAL_LABEL_ES
from willay.ports.signals import SearchResult

NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
HOURS = timedelta(hours=1)
ENDPOINTS = ("/signals", "/signals/refresh", "/signals/simulated")


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeSearch:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def search(self, query, *, since, max_results=5):
        self.calls += 1
        return list(self.results)


class FakeExtractor:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def extract_json(self, **kwargs):
        self.calls += 1
        return {"events": list(self.events)}


def page(n: int) -> SearchResult:
    return SearchResult(title=f"TITULO-{n}", url=f"https://diario.example.net/nota-{n}", published=NOW - 2 * HOURS, snippet=f"SNIPPET-{n}")


def event(n: int, place="SYNTHETIC Hamlet One") -> dict:
    return {
        "event_type": "huaico", "place_name": place, "district_ubigeo_guess": None,
        "event_time": (NOW - 3 * HOURS).isoformat(), "confidence": 0.8, "source_url": f"https://diario.example.net/nota-{n}",
    }


def make_deps(live: bool = False, pages: int = 3, clock: Clock | None = None):
    deps = synthetic_dependencies()
    settlements = [
        dataclasses.replace(s, ubigeo="150701", district="MATUCANA") if i == 0 else s for i, s in enumerate(deps.repository.list_settlements())
    ]
    deps.repository = InMemorySiteRepository(deps.repository.list_sites(), settlements)
    clock = clock or Clock()
    deps.signals = PublicSignalsHub(enabled=live, clock=clock, simulated_loader=lambda now: load_simulated_signals(now=now))
    if live:
        deps.signal_search = FakeSearch([page(n) for n in range(1, pages + 1)])
        deps.extractor = FakeExtractor([event(n) for n in range(1, pages + 1)])
    deps.signals_cooldown_s = 300.0
    return deps, clock


def client_for(live: bool = False, pages: int = 3):
    deps, clock = make_deps(live, pages)
    return TestClient(create_app(deps)), deps, clock


def test_the_endpoints_are_documented():
    client, _, _ = client_for()
    paths = client.get("/openapi.json").json()["paths"]
    assert all(path in paths for path in ENDPOINTS)


def test_signals_are_off_by_default_and_say_so():
    client, _, _ = client_for()
    body = client.get("/signals").json()
    assert body["mode"] == "off" and body["signals"] == [] and body["districts"] == []
    assert body["label"] == SIGNAL_LABEL_ES and "no verificadas" in body["disclaimer"]
    assert body["simulation_available"] is True and body["window_hours"] == 72


def test_the_simulated_set_is_labelled_and_can_be_switched_on_and_off():
    client, _, _ = client_for()
    on = client.post("/signals/simulated", json={"enabled": True}).json()
    assert on["mode"] == "simulated" and on["simulated"] is True and 5 <= len(on["signals"]) <= 8
    assert all(s["simulated"] is True and s["source_url"].startswith("https://example.org/") for s in on["signals"])
    assert all(d["simulated"] is True and d["label"] == SIGNAL_LABEL_ES for d in on["districts"])
    assert client.get("/signals").json()["mode"] == "simulated"
    filtered = client.get("/signals", params={"district": "lurigancho"}).json()
    assert {d["district"] for d in filtered["districts"]} == {"LURIGANCHO"} and filtered["signals"]
    off = client.post("/signals/simulated", json={"enabled": False}).json()
    assert off["mode"] == "off" and off["signals"] == []


def test_the_simulated_set_is_never_mixed_into_live_signals():
    client, _, _ = client_for(live=True)
    response = client.post("/signals/simulated", json={"enabled": True})
    assert response.status_code == 409
    assert client.get("/signals").json()["mode"] == "live"


def test_refresh_is_refused_while_signals_are_off_with_the_reason():
    client, _, _ = client_for()
    response = client.post("/signals/refresh")
    assert response.status_code == 503 and "WILLAY_SIGNALS_ENABLED" in response.json()["detail"]


def test_refresh_searches_extracts_geocodes_and_stores_only_the_minimal_record():
    client, deps, _ = client_for(live=True, pages=3)
    response = client.post("/signals/refresh")
    assert response.status_code == 200
    body = response.json()
    report = body["report"]
    assert (report["results_seen"], report["events_extracted"], report["signals_kept"], report["dropped_unmatched"]) == (3, 3, 3, 0)
    assert deps.signal_search.calls >= 1 and deps.extractor.calls == 1
    listing = client.get("/signals").json()
    assert listing["mode"] == "live" and listing["districts"][0]["count"] == 3 and listing["districts"][0]["ubigeo"] == "150701"
    text = json.dumps(listing, ensure_ascii=False)
    assert "TITULO-" not in text and "SNIPPET-" not in text
    assert {s["source_domain"] for s in listing["signals"]} == {"diario.example.net"}
    assert set(listing["signals"][0]) == {
        "event_type", "place", "ubigeo", "district", "lon", "lat", "event_time", "confidence", "source_url", "source_domain", "level", "simulated",
    }


def test_unmatched_places_are_dropped_in_the_refresh():
    deps, _ = make_deps(live=True, pages=2)
    deps.extractor = FakeExtractor([event(1), event(2, place="Barrio Que No Existe")])
    body = TestClient(create_app(deps)).post("/signals/refresh").json()
    assert body["report"]["signals_kept"] == 1 and body["report"]["dropped_unmatched"] == 1


def test_refreshing_again_too_soon_is_refused_until_the_cooldown_passes():
    client, deps, clock = client_for(live=True)
    assert client.post("/signals/refresh").status_code == 200
    again = client.post("/signals/refresh")
    assert again.status_code == 429 and "300" in again.json()["detail"]
    assert deps.extractor.calls == 1
    clock.now = NOW + 301 * timedelta(seconds=1)
    assert client.post("/signals/refresh").status_code == 200


def test_refresh_needs_the_extraction_model():
    deps, _ = make_deps(live=True)
    deps.extractor = None
    response = TestClient(create_app(deps)).post("/signals/refresh")
    assert response.status_code == 503 and "WILLAY_META_ENABLED" in response.json()["detail"]


def test_a_failing_search_is_reported_not_raised():
    deps, _ = make_deps(live=True)

    class Broken:
        def search(self, query, *, since, max_results=5):
            raise RuntimeError("down")

    deps.signal_search = Broken()
    body = TestClient(create_app(deps)).post("/signals/refresh").json()
    assert body["report"]["signals_kept"] == 0 and body["report"]["errors"]


# --- guardrails over HTTP ----------------------------------------------------------------------------------


def evaluate(client, **conditions):
    return client.post("/alerts/evaluate", json={"site_id": "syn-002", **conditions}).json()


def live_client_with_signals(pages=3):
    client, deps, _ = client_for(live=True, pages=pages)
    assert client.post("/signals/refresh").status_code == 200
    return client, deps


def test_attention_is_corroborated_by_signals_without_changing_the_tier():
    client, _ = live_client_with_signals(3)
    plain = client_for()[0]
    with_signals = evaluate(client, senamhi_level="orange")
    without = evaluate(plain, senamhi_level="orange")
    assert with_signals["tier"] == without["tier"] == "ATTENTION"
    assert with_signals["triggered"] == without["triggered"]
    assert any("corroborated by 3 public signals" in r for r in with_signals["reasons"])
    assert not any("public signals" in r for r in without["reasons"])


def test_signals_never_raise_a_tier_whatever_they_say():
    client, deps = live_client_with_signals(3)
    plain = client_for()[0]
    quiet = evaluate(client)
    assert quiet["tier"] == "NONE" and quiet["reasons"] == []
    rain = dict(senamhi_level="red", forecast_rain_72h_mm=90, observed_rain_24h_mm=35, water_rise_cm=50)
    pending_with, pending_without = evaluate(client, **rain), evaluate(plain, **rain)
    assert pending_with["tier"] == pending_without["tier"] == "PENDING_CONFIRMATION"
    assert pending_with["requires_operator_confirmation"] is True
    assert not any("public signals" in r for r in pending_with["reasons"])
    confirmed = evaluate(client, **rain, operator_confirmed=True)
    assert confirmed["tier"] == evaluate(plain, **rain, operator_confirmed=True)["tier"] == "EVACUATION"


def test_signals_never_dispatch_anything():
    client, deps = live_client_with_signals(5)
    assert list(deps.ledger.list_alerts()) == [] and deps.proposals.list() == []
    evaluate(client)
    assert client.get("/alerts/households").json() == client_for()[0].get("/alerts/households").json()
    assert list(deps.ledger.list_alerts()) == [] and deps.proposals.list() == []

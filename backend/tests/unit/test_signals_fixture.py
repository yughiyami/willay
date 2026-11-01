"""The offline fixture of SIMULATED public signals used by Demo mode: clearly fake, labelled, relative to 'now'."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from willay.adapters.signals_fixture import FIXTURE_FILE, SimulatedSignalsError, load_simulated_signals
from willay.domain.signals import SIGNAL_WINDOW_HOURS, aggregate_by_district
from willay.ports.signals import registered_host

NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)


def test_the_bundled_fixture_has_five_to_eight_simulated_signals_inside_the_window():
    signals = load_simulated_signals(now=NOW)
    assert 5 <= len(signals) <= 8
    assert all(s.simulated for s in signals)
    assert all(NOW - timedelta(hours=SIGNAL_WINDOW_HOURS) <= s.event_time <= NOW for s in signals)


def test_every_source_url_is_clearly_fake():
    for s in load_simulated_signals(now=NOW):
        assert s.source_url.startswith("https://example.org/") and registered_host(s.source_url) == "example.org"
        assert s.source_domain == "example.org"


def test_the_fixture_file_says_it_is_synthetic_and_carries_no_personal_fields():
    raw = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))
    assert raw["synthetic"] is True
    allowed = {"event_type", "place", "ubigeo", "district", "lon", "lat", "hours_ago", "confidence", "source_url", "level"}
    assert all(set(item) <= allowed for item in raw["signals"])


def test_times_follow_now_so_the_demo_never_goes_stale():
    later = NOW + timedelta(days=30)
    assert {s.event_time - NOW for s in load_simulated_signals(now=NOW)} == {s.event_time - later for s in load_simulated_signals(now=later)}


def test_at_least_one_district_has_two_pages_so_the_corroboration_can_be_shown_and_one_has_a_single_page():
    counts = sorted(d.count for d in aggregate_by_district(load_simulated_signals(now=NOW), NOW))
    assert counts[-1] >= 2 and counts[0] == 1


def test_a_file_that_is_not_flagged_synthetic_or_uses_real_looking_links_is_refused(tmp_path):
    item = {"event_type": "huaico", "place": "Chosica", "ubigeo": "150118", "district": "LURIGANCHO", "lon": -76.69, "lat": -11.93, "hours_ago": 3, "confidence": 0.7, "source_url": "https://example.org/simulado/1"}
    unflagged = tmp_path / "a.json"
    unflagged.write_text(json.dumps({"signals": [item]}), encoding="utf-8")
    with pytest.raises(SimulatedSignalsError):
        load_simulated_signals(unflagged, now=NOW)
    real = tmp_path / "b.json"
    real.write_text(json.dumps({"synthetic": True, "signals": [item | {"source_url": "https://rpp.pe/lima/huaico"}]}), encoding="utf-8")
    with pytest.raises(SimulatedSignalsError):
        load_simulated_signals(real, now=NOW)
    odd = tmp_path / "c.json"
    odd.write_text(json.dumps({"synthetic": True, "signals": [item | {"event_type": "meteorito"}]}), encoding="utf-8")
    with pytest.raises(SimulatedSignalsError):
        load_simulated_signals(odd, now=NOW)

"""Public signals use cases: queries, extraction with a mocked model, deterministic geocoding, privacy and the hub."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from willay.application.public_signals import (
    EXTRACTION_SCHEMA,
    MIN_CONFIDENCE,
    PublicSignalsHub,
    build_gazetteer,
    build_queries,
    clean_url,
    extract_events,
    refresh_public_signals,
    signals_from_events,
)
from willay.domain.entities import Settlement
from willay.domain.signals import SIGNAL_LABEL_ES, Gazetteer, PublicSignal, SignalEventType
from willay.ports.signals import SearchResult

NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
HOURS = timedelta(hours=1)


def settlement(sid, name, ubigeo, district, lon=-76.7, lat=-11.9) -> Settlement:
    return Settlement(id=sid, name=name, lon=lon, lat=lat, population=100, households=0, ubigeo=ubigeo, district=district)


SETTLEMENTS = [
    settlement("ana-1", "CHOSICA", "150118", "LURIGANCHO", -76.69, -11.93),
    settlement("ana-2", "ÑAÑA", "150118", "LURIGANCHO", -76.8, -11.98),
    settlement("ana-3", "MATUCANA", "150701", "MATUCANA", -76.38, -11.85),
    settlement("ana-4", "SANTA ROSA", "150118", "LURIGANCHO", -76.6, -11.9),
    settlement("ana-5", "SANTA ROSA", "150722", "SAN MATEO", -76.3, -11.8),
    settlement("ana-6", "Unnamed settlement", "150118", "LURIGANCHO"),
]
DISTRICTS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[[-76.9, -12.0], [-76.5, -12.0], [-76.5, -11.8], [-76.9, -11.8], [-76.9, -12.0]]]},
            "properties": {"ubigeo": "150118", "name": "LURIGANCHO"},
        },
        {"type": "Feature", "geometry": None, "properties": {"ubigeo": "150722", "name": "SAN MATEO"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {"ubigeo": "150999", "name": "ODD"}},
    ],
}


def result(url="https://diario.example.org/a", published=NOW - 3 * HOURS, title="Huaico en Chosica", snippet="Un huaico bajó anoche.") -> SearchResult:
    return SearchResult(title=title, url=url, published=published, snippet=snippet)


def raw_event(**overrides) -> dict:
    event = {
        "event_type": "huaico",
        "place_name": "Chosica",
        "district_ubigeo_guess": "150118",
        "event_time": (NOW - 4 * HOURS).isoformat(),
        "confidence": 0.8,
        "source_url": "https://diario.example.org/a",
    }
    event.update(overrides)
    return event


class FakeExtractor:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def extract_json(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {"events": []}
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class FakeSearch:
    by_query: dict = field(default_factory=dict)
    default: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    sinces: list = field(default_factory=list)

    def search(self, query, *, since, max_results=5):
        self.queries.append(query)
        self.sinces.append(since)
        found = self.by_query.get(query, self.default)
        if isinstance(found, Exception):
            raise found
        return list(found)


# --- gazetteer from our own data -----------------------------------------------------------------


def test_the_gazetteer_holds_settlements_and_districts_and_skips_unnamed_places():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    assert gazetteer.lookup("chosica").ubigeo == "150118"
    assert gazetteer.lookup("Ñaña").place == "ÑAÑA"
    assert gazetteer.lookup("Unnamed settlement") is None
    district = gazetteer.lookup("Lurigancho")
    assert district.level == "district" and district.ubigeo == "150118"
    assert (district.lon, district.lat) == pytest.approx((-76.7, -11.9))  # centre of the polygon's bounding box


def test_a_district_without_geometry_is_placed_at_the_centre_of_its_settlements():
    match = build_gazetteer(SETTLEMENTS, DISTRICTS).lookup("San Mateo")
    assert match.level == "district" and (match.lon, match.lat) == pytest.approx((-76.3, -11.8))


def test_a_district_missing_from_the_collection_is_built_from_its_settlements_and_geometry_that_is_not_a_polygon_is_ignored():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    assert gazetteer.lookup("Matucana", "150701") is not None
    assert gazetteer.lookup("Odd") is None or gazetteer.lookup("Odd").level == "district"


def test_the_same_name_in_two_districts_is_dropped_without_a_guess():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    assert gazetteer.lookup("Santa Rosa") is None
    assert gazetteer.lookup("Santa Rosa", "150722").district == "SAN MATEO"


def test_gazetteer_works_without_a_district_collection():
    gazetteer = build_gazetteer(SETTLEMENTS, None)
    assert gazetteer.lookup("Lurigancho").level == "district"


# --- queries -----------------------------------------------------------------------------------------


def test_queries_cover_the_five_hazards_and_group_the_basin_places():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    queries = build_queries(gazetteer, max_queries=8, group_size=2)
    assert queries
    for term in ("huaico", "deslizamiento", "desborde", "relave", "inundación"):
        assert all(term in q for q in queries)
    joined = " ".join(queries).lower()
    assert "lurigancho" in joined and "matucana" in joined and "san mateo" in joined
    assert "unnamed" not in joined


def test_the_number_of_queries_is_capped():
    many = [settlement(f"s{i}", f"PUEBLO {i}", f"15{i:04d}", f"DISTRITO {i}") for i in range(40)]
    queries = build_queries(build_gazetteer(many, None), max_queries=3, group_size=5)
    assert len(queries) == 3


def test_no_places_means_no_queries():
    assert build_queries(Gazetteer([]), max_queries=8) == []


# --- extraction with the model mocked ------------------------------------------------------------------


def test_the_model_gets_only_title_url_date_and_snippet_and_a_strict_contract():
    extractor = FakeExtractor({"events": []})
    extract_events([result()], extractor, NOW)
    call = extractor.calls[0]
    assert call["schema_name"] == "public_signal_events" and call["schema"] == EXTRACTION_SCHEMA
    assert call["model"] == "muse-spark-1.3"
    text = " ".join(part["text"] for part in call["content"] if part["type"] == "input_text")
    sent = json.loads(text[text.index("["):])
    assert sent == [{"url": "https://diario.example.org/a", "title": "Huaico en Chosica", "published": (NOW - 3 * HOURS).isoformat(), "snippet": "Un huaico bajó anoche."}]
    assert "personal" in call["instructions"].lower() and "72" in call["instructions"]


def test_the_schema_asks_for_exactly_the_six_fields():
    item = EXTRACTION_SCHEMA["properties"]["events"]["items"]
    assert set(item["properties"]) == {"event_type", "place_name", "district_ubigeo_guess", "event_time", "confidence", "source_url"}
    assert set(item["properties"]["event_type"]["enum"]) == {"huaico", "deslizamiento", "desborde", "relave", "inundacion"}


def test_a_valid_event_is_parsed():
    events = extract_events([result()], FakeExtractor({"events": [raw_event()]}), NOW)
    assert len(events) == 1
    event = events[0]
    assert event.event_type is SignalEventType.HUAICO and event.place_name == "Chosica" and event.ubigeo_guess == "150118"
    assert event.event_time == NOW - 4 * HOURS and event.confidence == 0.8 and event.source_url == "https://diario.example.org/a"


@pytest.mark.parametrize(
    "bad",
    [
        {"event_type": "terremoto"},
        {"event_type": None},
        {"source_url": "https://invented.example.org/never-searched"},
        {"confidence": MIN_CONFIDENCE - 0.01},
        {"confidence": "alta"},
        {"confidence": 1.5},
        {"place_name": ""},
        {"place_name": None},
        {"event_time": (NOW - 80 * HOURS).isoformat()},
        {"event_time": (NOW + 6 * HOURS).isoformat()},
    ],
)
def test_events_that_break_the_contract_are_dropped(bad):
    assert extract_events([result()], FakeExtractor({"events": [raw_event(**bad)]}), NOW) == []


def test_without_an_event_time_the_page_publication_date_is_used_else_the_event_is_dropped():
    no_time = raw_event(event_time=None)
    assert extract_events([result()], FakeExtractor({"events": [no_time]}), NOW)[0].event_time == NOW - 3 * HOURS
    assert extract_events([result(published=None)], FakeExtractor({"events": [no_time]}), NOW) == []
    garbled = raw_event(event_time="ayer por la tarde")
    assert extract_events([result()], FakeExtractor({"events": [garbled]}), NOW)[0].event_time == NOW - 3 * HOURS


def test_a_naive_event_time_is_taken_as_utc():
    naive = raw_event(event_time=(NOW - 2 * HOURS).replace(tzinfo=None).isoformat())
    assert extract_events([result()], FakeExtractor({"events": [naive]}), NOW)[0].event_time == NOW - 2 * HOURS


def test_extra_fields_the_model_adds_are_never_carried():
    sneaky = raw_event(author="Maria Lopez", handle="@maria", profile_url="https://x.com/maria", text="texto original")
    event = extract_events([result()], FakeExtractor({"events": [sneaky]}), NOW)[0]
    dump = repr(event)
    assert "Maria" not in dump and "@maria" not in dump and "texto original" not in dump
    assert set(vars(event)) == {"event_type", "place_name", "ubigeo_guess", "event_time", "confidence", "source_url"}


def test_a_page_with_no_snippet_and_pages_beyond_the_cap_are_handled():
    many = [result(url=f"https://diario.example.org/{i}") for i in range(23)]
    extractor = FakeExtractor()
    extract_events(many, extractor, NOW)
    assert len(extractor.calls) == 3  # batches of 10, so cost stays bounded
    assert extract_events([], FakeExtractor(), NOW) == []


def test_malformed_model_output_gives_no_events_instead_of_crashing():
    for answer in ({}, {"events": "none"}, {"events": [1, "x", None]}, {"events": [{}]}):
        assert extract_events([result()], FakeExtractor(answer), NOW) == []


# --- deterministic geocoding, dedupe and privacy ------------------------------------------------------------


def events_from(*raws):
    return extract_events([result()], FakeExtractor({"events": list(raws)}), NOW)


def test_matched_events_become_signals_with_canonical_place_and_coordinates():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    signals, dropped = signals_from_events(events_from(raw_event(place_name="  chosica ")), gazetteer)
    assert dropped == 0 and len(signals) == 1
    signal = signals[0]
    assert (signal.place, signal.ubigeo, signal.district) == ("CHOSICA", "150118", "LURIGANCHO")
    assert (signal.lon, signal.lat) == (-76.69, -11.93)
    assert signal.source_domain == "diario.example.org" and not signal.simulated


def test_unmatched_places_are_dropped_and_counted():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    signals, dropped = signals_from_events(events_from(raw_event(place_name="Barrio Imaginario"), raw_event(place_name="Chosic")), gazetteer)
    assert signals == [] and dropped == 2


def test_the_stored_place_is_ours_not_the_raw_text():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    signals, _ = signals_from_events(events_from(raw_event(place_name="CHOSICA!!")), gazetteer)
    assert signals[0].place == "CHOSICA"


def test_one_page_one_place_one_event_type_is_kept_once():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    signals, _ = signals_from_events(events_from(raw_event(), raw_event(place_name="Chosica")), gazetteer)
    assert len(signals) == 1


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://user:secret@news.example.org/a?utm_source=x&id=7#frag", "https://news.example.org/a?id=7"),
        ("https://news.example.org/a?fbclid=abc&gclid=1&utm_campaign=z", "https://news.example.org/a"),
        ("http://News.Example.org:8080/a/b", "http://news.example.org:8080/a/b"),
        ("https://news.example.org", "https://news.example.org/"),
    ],
)
def test_clean_url_keeps_domain_and_path_and_drops_credentials_trackers_and_fragments(url, expected):
    assert clean_url(url) == expected


def test_social_and_non_web_urls_never_become_signals():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    pages = [
        result(url="https://www.facebook.com/groups/x/posts/1"),
        result(url="https://twitter.com/someone/status/2"),
        result(url="ftp://files.example.org/a"),
        result(url="https://diario.example.org/a"),
    ]
    raws = [raw_event(source_url=p.url) for p in pages]
    events = extract_events(pages, FakeExtractor({"events": raws}), NOW)
    signals, _ = signals_from_events(events, gazetteer)
    assert [s.source_url for s in signals] == ["https://diario.example.org/a"]


# --- refresh: search -> extract -> geocode -> hub -------------------------------------------------------------


def make_hub(**kwargs) -> PublicSignalsHub:
    return PublicSignalsHub(enabled=True, clock=lambda: NOW, **kwargs)


def test_refresh_searches_the_last_72_hours_extracts_geocodes_and_discards_the_raw_text():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    search = FakeSearch(default=[result(snippet="SNIPPET-SECRETO de la nota"), result(url="https://otro.example.org/b", title="TITULO-PERSONAL de Juan")])
    extractor = FakeExtractor({"events": [raw_event(), raw_event(source_url="https://otro.example.org/b", place_name="Ñaña", event_type="desborde"), raw_event(place_name="Lugar Inventado")]})
    hub = make_hub()
    report = refresh_public_signals(search, extractor, gazetteer, hub, now=NOW, max_queries=1)
    assert search.sinces == [NOW - timedelta(hours=72)]
    assert (report.results_seen, report.events_extracted, report.signals_kept, report.dropped_unmatched) == (2, 3, 2, 1)
    assert report.errors == []
    snapshot = json.dumps(hub.snapshot(), ensure_ascii=False)
    assert "SNIPPET-SECRETO" not in snapshot and "TITULO-PERSONAL" not in snapshot and "Juan" not in snapshot
    assert SIGNAL_LABEL_ES in snapshot
    assert "SNIPPET-SECRETO" not in repr(hub.__dict__) and "TITULO-PERSONAL" not in repr(hub.__dict__)


def test_a_failing_search_or_extraction_is_reported_and_the_rest_continues():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    queries = build_queries(gazetteer, max_queries=2, group_size=1)
    search = FakeSearch(by_query={queries[0]: RuntimeError("boom"), queries[1]: [result()]})
    hub = make_hub()
    report = refresh_public_signals(search, FakeExtractor({"events": [raw_event()]}), gazetteer, hub, now=NOW, max_queries=2, group_size=1)
    assert report.signals_kept == 1 and len(report.errors) == 1 and "RuntimeError" in report.errors[0] and "boom" not in report.errors[0]
    failing = refresh_public_signals(FakeSearch(default=[result()]), FakeExtractor(RuntimeError("model down")), gazetteer, make_hub(), now=NOW, max_queries=1)
    assert failing.signals_kept == 0 and failing.errors and "model down" not in failing.errors[0]


def test_results_older_than_the_window_are_not_sent_to_the_model():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    stale = result(url="https://old.example.org/x", published=NOW - 100 * HOURS)
    extractor = FakeExtractor({"events": []})
    refresh_public_signals(FakeSearch(default=[stale]), extractor, gazetteer, make_hub(), now=NOW, max_queries=1)
    assert extractor.calls == []


def test_the_same_page_found_by_two_queries_is_extracted_once():
    gazetteer = build_gazetteer(SETTLEMENTS, DISTRICTS)
    extractor = FakeExtractor({"events": [raw_event()]})
    report = refresh_public_signals(FakeSearch(default=[result()]), extractor, gazetteer, make_hub(), now=NOW, max_queries=3, group_size=1)
    assert report.results_seen == 1 and len(extractor.calls) == 1


# --- the hub ---------------------------------------------------------------------------------------------


def sample_signal(**overrides) -> PublicSignal:
    values = dict(
        event_type=SignalEventType.HUAICO, place="CHOSICA", ubigeo="150118", district="LURIGANCHO", lon=-76.69, lat=-11.93,
        event_time=NOW - 2 * HOURS, confidence=0.8, source_url="https://diario.example.org/a", source_domain="diario.example.org",
    )
    values.update(overrides)
    return PublicSignal(**values)


def test_a_hub_that_is_off_is_empty_and_says_so():
    hub = PublicSignalsHub(clock=lambda: NOW)
    snapshot = hub.snapshot()
    assert snapshot["mode"] == "off" and snapshot["signals"] == [] and snapshot["districts"] == [] and snapshot["label"] == SIGNAL_LABEL_ES
    assert hub.count_for(ubigeos={"150118"}, districts=set()) == 0


def test_live_snapshot_aggregates_per_district_with_links_and_labels():
    hub = make_hub()
    hub.replace([sample_signal(), sample_signal(source_url="https://b.example.org/2", source_domain="b.example.org", event_type=SignalEventType.OVERFLOW)], refreshed_at=NOW)
    snapshot = hub.snapshot()
    assert snapshot["mode"] == "live" and snapshot["simulated"] is False and snapshot["refreshed_at"] == NOW.isoformat()
    district = snapshot["districts"][0]
    assert district["count"] == 2 and district["ubigeo"] == "150118" and district["label"] == SIGNAL_LABEL_ES
    assert {s["url"] for s in district["sources"]} == {"https://diario.example.org/a", "https://b.example.org/2"}
    assert {s["event_type"] for s in snapshot["signals"]} == {"huaico", "desborde"}
    assert snapshot["window_hours"] == 72 and "no verificadas" in snapshot["disclaimer"]


def test_merging_keeps_earlier_signals_still_in_the_window_and_drops_the_expired():
    hub = make_hub()
    hub.replace([sample_signal(source_url="https://a.example.org/old", event_time=NOW - 60 * HOURS)], refreshed_at=NOW)
    later = NOW + 20 * HOURS
    hub2 = PublicSignalsHub(enabled=True, clock=lambda: later)
    hub2.replace([sample_signal(source_url="https://a.example.org/old", event_time=NOW - 60 * HOURS)], refreshed_at=NOW)
    assert hub2.snapshot()["signals"] == []  # 80 hours old now
    hub.merge([sample_signal(source_url="https://a.example.org/new")], refreshed_at=NOW)
    assert len(hub.snapshot()["signals"]) == 2


def test_simulated_mode_only_when_live_is_not_enabled_and_is_labelled():
    simulated = [sample_signal(simulated=True, source_url="https://example.org/simulado/1")]
    hub = PublicSignalsHub(clock=lambda: NOW, simulated_loader=lambda now: simulated)
    hub.set_simulated(True)
    snapshot = hub.snapshot()
    assert snapshot["mode"] == "simulated" and snapshot["simulated"] is True and len(snapshot["signals"]) == 1
    assert snapshot["signals"][0]["simulated"] is True and snapshot["districts"][0]["simulated"] is True
    hub.set_simulated(False)
    assert hub.snapshot()["mode"] == "off"
    live = PublicSignalsHub(enabled=True, clock=lambda: NOW, simulated_loader=lambda now: simulated)
    live.set_simulated(True)  # live data wins: the fixture never mixes with it
    assert live.snapshot()["mode"] == "live" and live.snapshot()["signals"] == []


def test_count_for_counts_pages_by_ubigeo_or_district_name():
    hub = make_hub()
    hub.replace(
        [
            sample_signal(),
            sample_signal(source_url="https://b.example.org/2", source_domain="b.example.org"),
            sample_signal(ubigeo="150701", district="MATUCANA", source_url="https://c.example.org/3", source_domain="c.example.org"),
            sample_signal(ubigeo="", district="San Mateo", source_url="https://d.example.org/4", source_domain="d.example.org"),
        ],
        refreshed_at=NOW,
    )
    assert hub.count_for(ubigeos={"150118"}, districts=set()) == 2
    assert hub.count_for(ubigeos={"150118", "150701"}, districts=set()) == 3
    assert hub.count_for(ubigeos=set(), districts={"lurigancho"}) == 2
    assert hub.count_for(ubigeos=set(), districts={"san mateo"}) == 1
    assert hub.count_for(ubigeos={"150118"}, districts={"lurigancho"}) == 2  # a page is never counted twice


def test_district_filter_on_the_snapshot_matches_name_or_ubigeo():
    hub = make_hub()
    hub.replace([sample_signal(), sample_signal(ubigeo="150701", district="MATUCANA", source_url="https://c.example.org/3", source_domain="c.example.org")], refreshed_at=NOW)
    assert [d["ubigeo"] for d in hub.snapshot(district="matucana")["districts"]] == ["150701"]
    assert [d["ubigeo"] for d in hub.snapshot(district="150118")["districts"]] == ["150118"]
    assert hub.snapshot(district="nowhere")["signals"] == []

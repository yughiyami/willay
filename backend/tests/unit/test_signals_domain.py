"""Public signals: normalization, exact geocoding, aggregation and the guardrails (domain, pure Python)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from willay.domain.entities import AlertTier, EnfenMagnitude, HazardContext, RainfallAnomaly, SenamhiLevel
from willay.domain.signals import (
    SIGNAL_LABEL_ES,
    SIGNAL_WINDOW_HOURS,
    Gazetteer,
    GazetteerEntry,
    PublicSignal,
    SignalEventType,
    aggregate_by_district,
    corroborate,
    normalize_place,
)
from willay.domain.tiers import evaluate_tier

NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)


def signal(**overrides) -> PublicSignal:
    values = dict(
        event_type=SignalEventType.HUAICO,
        place="Chosica",
        ubigeo="150118",
        district="LURIGANCHO",
        lon=-76.69,
        lat=-11.93,
        event_time=NOW - timedelta(hours=5),
        confidence=0.8,
        source_url="https://news.example.org/a",
        source_domain="news.example.org",
    )
    values.update(overrides)
    return PublicSignal(**values)


# --- labels and normalization -------------------------------------------------------------------


def test_signals_carry_the_unverified_label():
    assert SIGNAL_LABEL_ES == "señales públicas no verificadas"
    assert SIGNAL_WINDOW_HOURS == 72


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Chosica", "chosica"),
        ("  CHOSICA  ", "chosica"),
        ("Santa  Eulalia", "santa eulalia"),
        ("Ñaña", "nana"),
        ("San Bartolomé", "san bartolome"),
        ("Ricardo Palma.", "ricardo palma"),
        ("Matucana,", "matucana"),
        ("Jicamarca (Huarochirí)", "jicamarca huarochiri"),
        ("", ""),
    ],
)
def test_normalize_place_strips_case_accents_punctuation_and_spaces(raw, expected):
    assert normalize_place(raw) == expected


# --- geocoding: exact normalized match only -----------------------------------------------------


def entries() -> list[GazetteerEntry]:
    return [
        GazetteerEntry("Chosica", "150118", "LURIGANCHO", -76.69, -11.93, "settlement"),
        GazetteerEntry("SAN BARTOLOME", "150717", "SAN BARTOLOME", -76.5, -11.9, "district"),
        GazetteerEntry("LURIGANCHO", "150118", "LURIGANCHO", -76.7, -11.95, "district"),
        GazetteerEntry("Santa Rosa", "150118", "LURIGANCHO", -76.6, -11.9, "settlement"),
        GazetteerEntry("Santa Rosa", "150722", "SAN MATEO", -76.3, -11.8, "settlement"),
    ]


def test_exact_normalized_match_returns_the_canonical_entry():
    gazetteer = Gazetteer(entries())
    match = gazetteer.lookup("  chosica ")
    assert match is not None and (match.place, match.ubigeo, match.district) == ("Chosica", "150118", "LURIGANCHO")
    assert gazetteer.lookup("San Bartolomé").ubigeo == "150717"


@pytest.mark.parametrize("name", ["Chosic", "Chosica Alta", "Rio Rimac", "", "   ", "Lima"])
def test_unmatched_places_are_dropped_not_fuzzy_matched(name):
    assert Gazetteer(entries()).lookup(name) is None


def test_an_ambiguous_name_needs_a_ubigeo_guess_that_picks_one_district():
    gazetteer = Gazetteer(entries())
    assert gazetteer.lookup("Santa Rosa") is None
    assert gazetteer.lookup("Santa Rosa", ubigeo_guess="150722").district == "SAN MATEO"
    assert gazetteer.lookup("Santa Rosa", ubigeo_guess="999999") is None


def test_the_ubigeo_guess_never_creates_a_match_by_itself():
    assert Gazetteer(entries()).lookup("Lugar inexistente", ubigeo_guess="150118") is None


def test_several_settlements_with_one_name_in_one_district_resolve_to_the_district_level():
    gazetteer = Gazetteer(
        [
            GazetteerEntry("El Progreso", "150118", "LURIGANCHO", -76.1, -11.1, "settlement"),
            GazetteerEntry("El Progreso", "150118", "LURIGANCHO", -76.3, -11.3, "settlement"),
            GazetteerEntry("LURIGANCHO", "150118", "LURIGANCHO", -76.7, -11.95, "district"),
        ]
    )
    match = gazetteer.lookup("El Progreso")
    assert match is not None and match.level == "district" and (match.lon, match.lat) == (-76.7, -11.95)


# --- aggregation per district -------------------------------------------------------------------


def test_aggregate_counts_latest_time_and_source_links_per_district():
    signals = [
        signal(source_url="https://a.example.org/1", source_domain="a.example.org", event_time=NOW - timedelta(hours=30)),
        signal(source_url="https://b.example.org/2", source_domain="b.example.org", event_time=NOW - timedelta(hours=2)),
        signal(ubigeo="150717", district="SAN BARTOLOME", place="San Bartolomé", source_url="https://c.example.org/3", source_domain="c.example.org"),
    ]
    result = aggregate_by_district(signals, NOW)
    by_ubigeo = {d.ubigeo: d for d in result}
    assert by_ubigeo["150118"].count == 2
    assert by_ubigeo["150118"].latest == NOW - timedelta(hours=2)
    assert {s.url for s in by_ubigeo["150118"].sources} == {"https://a.example.org/1", "https://b.example.org/2"}
    assert by_ubigeo["150717"].count == 1
    assert [d.ubigeo for d in result][0] == "150118"  # most reported district first


def test_the_same_page_is_counted_once_per_district():
    twice = [signal(event_type=SignalEventType.HUAICO), signal(event_type=SignalEventType.FLOOD)]
    assert aggregate_by_district(twice, NOW)[0].count == 1


def test_signals_outside_the_72_hour_window_or_in_the_future_are_ignored():
    old = signal(source_url="https://x.example.org/old", event_time=NOW - timedelta(hours=SIGNAL_WINDOW_HOURS + 1))
    future = signal(source_url="https://x.example.org/future", event_time=NOW + timedelta(hours=3))
    edge = signal(source_url="https://x.example.org/edge", event_time=NOW - timedelta(hours=SIGNAL_WINDOW_HOURS))
    result = aggregate_by_district([old, future, edge], NOW)
    assert result[0].count == 1 and result[0].sources[0].url == "https://x.example.org/edge"
    assert aggregate_by_district([old, future], NOW) == ()


def test_district_aggregates_are_labeled_and_carry_no_personal_fields():
    result = aggregate_by_district([signal()], NOW)[0]
    assert result.label == SIGNAL_LABEL_ES
    fields = set(vars(result)) | set(vars(result.sources[0])) | set(vars(signal()))
    assert not fields & {"author", "handle", "username", "profile_url", "text", "snippet", "title", "raw_text"}


# --- guardrails: signals corroborate ATTENTION and nothing else ---------------------------------

QUIET = HazardContext()
ATTENTION_CTX = HazardContext(senamhi_level=SenamhiLevel.ORANGE)
PREPARATION_CTX = HazardContext(enfen_magnitude=EnfenMagnitude.EXTRAORDINARY, seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL)
EVACUATION_CONDITIONS = HazardContext(
    senamhi_level=SenamhiLevel.RED, forecast_rain_72h_mm=90.0, observed_rain_24h_mm=35.0, water_rise_cm=50.0
)

ALL_CONTEXTS = [
    QUIET,
    ATTENTION_CTX,
    PREPARATION_CTX,
    replace(PREPARATION_CTX, senamhi_level=SenamhiLevel.ORANGE),
    EVACUATION_CONDITIONS,
    replace(EVACUATION_CONDITIONS, operator_confirmed=True),
    replace(ATTENTION_CTX, observed_rain_24h_mm=35.0, watcher_report=True),
]


def test_attention_gets_the_corroboration_reason_with_the_count():
    base = evaluate_tier(ATTENTION_CTX, 0.2)
    corroborated = corroborate(base, 3)
    assert corroborated.tier is AlertTier.ATTENTION
    assert any("corroborated by 3 public signals" in reason for reason in corroborated.reasons)
    assert SIGNAL_LABEL_ES in corroborated.reasons[-1]
    assert corroborated.reasons[: len(base.reasons)] == base.reasons  # the rule reasons are untouched


def test_the_corroboration_reason_needs_a_minimum_of_independent_pages():
    base = evaluate_tier(ATTENTION_CTX, 0.2)
    assert corroborate(base, 1) == base
    assert corroborate(base, 0) == base
    assert corroborate(base, 1, min_signals=1) != base


@pytest.mark.parametrize("context", ALL_CONTEXTS)
@pytest.mark.parametrize("count", [0, 1, 2, 5, 1000])
def test_signals_never_change_a_tier_or_what_triggered_it(context, count):
    base = evaluate_tier(context, 0.2)
    with_signals = corroborate(base, count)
    assert with_signals.tier is base.tier
    assert with_signals.triggered == base.triggered


@pytest.mark.parametrize("count", [0, 1, 2, 50, 10_000])
def test_signals_alone_never_reach_evacuation_or_pending_confirmation(count):
    for context in (QUIET, ATTENTION_CTX, PREPARATION_CTX):
        tier = corroborate(evaluate_tier(context, 0.9), count).tier
        assert tier not in (AlertTier.EVACUATION, AlertTier.PENDING_CONFIRMATION)


def test_signals_do_not_create_a_tier_out_of_nothing():
    quiet = corroborate(evaluate_tier(QUIET, 0.2), 99)
    assert quiet.tier is AlertTier.NONE and quiet.reasons == ()
    prep = corroborate(evaluate_tier(PREPARATION_CTX, 0.9), 99)
    assert prep.tier is AlertTier.PREPARATION and not any("public signals" in r for r in prep.reasons)


def test_evacuation_conditions_without_the_operator_stay_pending_whatever_the_signals_say():
    result = corroborate(evaluate_tier(EVACUATION_CONDITIONS, 0.2), 500)
    assert result.tier is AlertTier.PENDING_CONFIRMATION
    assert not any("public signals" in r for r in result.reasons)

"""Public signals: crowd/news corroboration of hazard events. Pure Python.

A public signal is a report found on the open web (news, public pages) that something happened in a place of the Rimac basin
in the last 72 hours: a huaico, a landslide, an overflow, a tailings spill, a flood. It is **unverified** and it is context,
never a trigger:

  * signals can add one reason to an ATTENTION evaluation ("corroborated by N public signals");
  * signals never create, raise or lower a tier, never reach PENDING_CONFIRMATION or EVACUATION, and never start a dispatch.
    `corroborate` takes an evaluation and returns one with the same tier by construction (tested for every tier);
  * a signal holds the event type, the place, the time and the source page (URL and domain) and nothing else: no text, no
    author, no handle, no profile URL.

Geocoding is deterministic and exact: a place name is matched, after normalization, against our own settlements and districts.
A name that matches nothing, or matches several districts, is dropped. Nothing is fuzzy-matched or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from willay.domain.entities import AlertTier
from willay.domain.tiers import TierEvaluation

SIGNAL_LABEL_ES = "señales públicas no verificadas"
SIGNAL_WINDOW_HOURS = 72
MIN_CORROBORATING_SIGNALS = 2  # independent pages needed before a reason is added
_FUTURE_TOLERANCE = timedelta(hours=1)  # publication times in the wrong time zone are common; anything later is dropped


class SignalEventType(Enum):
    HUAICO = "huaico"
    LANDSLIDE = "deslizamiento"
    OVERFLOW = "desborde"
    TAILINGS = "relave"
    FLOOD = "inundacion"


# --- place names ---------------------------------------------------------------------------------------

_ACCENTS = str.maketrans("áàäâéèëêíìïîóòöôúùüûñ", "aaaaeeeeiiiioooouuuun")


def normalize_place(text: str) -> str:
    """Lower case, no accents or punctuation, single spaces. 'Ñaña' -> 'nana', 'San Bartolomé,' -> 'san bartolome'."""
    folded = (text or "").lower().translate(_ACCENTS)
    return " ".join("".join(c if c.isalnum() else " " for c in folded).split())


@dataclass(frozen=True)
class GazetteerEntry:
    place: str  # canonical name as we hold it (settlement or district)
    ubigeo: str  # may be empty when the source does not carry it
    district: str
    lon: float
    lat: float
    kind: str = "settlement"  # settlement | district


@dataclass(frozen=True)
class GeoMatch:
    place: str
    ubigeo: str
    district: str
    lon: float
    lat: float
    level: str  # settlement | district


class Gazetteer:
    """Our own place names: exact normalized lookup, no fuzzy matching."""

    def __init__(self, entries: list[GazetteerEntry] | tuple[GazetteerEntry, ...]) -> None:
        self._entries = tuple(entries)
        self._by_name: dict[str, list[GazetteerEntry]] = {}
        self._district_entry: dict[str, GazetteerEntry] = {}
        for entry in self._entries:
            key = normalize_place(entry.place)
            if key:
                self._by_name.setdefault(key, []).append(entry)
            if entry.kind == "district" and entry.ubigeo:
                self._district_entry.setdefault(entry.ubigeo, entry)

    def __len__(self) -> int:
        return len(self._entries)

    def place_names(self) -> list[str]:
        """Canonical district names, for building search queries."""
        return sorted({e.place for e in self._entries if e.kind == "district"})

    def lookup(self, place_name: str, ubigeo_guess: str | None = None) -> GeoMatch | None:
        key = normalize_place(place_name)
        candidates = self._by_name.get(key, []) if key else []
        if not candidates:
            return None
        groups: dict[str, list[GazetteerEntry]] = {}
        for candidate in candidates:
            groups.setdefault(candidate.ubigeo or candidate.district, []).append(candidate)
        if len(groups) > 1:
            # The same name in several districts: only a guess that picks exactly one of them can resolve it.
            guess = (ubigeo_guess or "").strip()
            chosen = groups.get(guess)
            if chosen is None:
                return None
            group = chosen
        else:
            group = next(iter(groups.values()))
        if len(group) == 1:
            only = group[0]
            return GeoMatch(only.place, only.ubigeo, only.district, only.lon, only.lat, only.kind)
        # Several places with this name inside one district: report the district, not an arbitrary one of them.
        first = group[0]
        district = self._district_entry.get(first.ubigeo)
        if district is None:
            lon = sum(e.lon for e in group) / len(group)
            lat = sum(e.lat for e in group) / len(group)
            return GeoMatch(first.district or first.place, first.ubigeo, first.district, lon, lat, "district")
        return GeoMatch(district.place, district.ubigeo, district.district, district.lon, district.lat, "district")


# --- signals and aggregates ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicSignal:
    event_type: SignalEventType
    place: str  # canonical place name from our gazetteer, never the raw text
    ubigeo: str
    district: str
    lon: float
    lat: float
    event_time: datetime
    confidence: float  # the extractor's confidence that the page reports this event; not a measure of truth
    source_url: str
    source_domain: str
    level: str = "settlement"
    simulated: bool = False


@dataclass(frozen=True)
class SignalSource:
    url: str
    domain: str
    event_type: SignalEventType
    event_time: datetime


@dataclass(frozen=True)
class DistrictSignals:
    ubigeo: str
    district: str
    count: int  # distinct source pages
    latest: datetime
    sources: tuple[SignalSource, ...]
    lon: float
    lat: float
    label: str = SIGNAL_LABEL_ES
    simulated: bool = False


def in_window(event_time: datetime, now: datetime, window_hours: int = SIGNAL_WINDOW_HOURS) -> bool:
    return now - timedelta(hours=window_hours) <= event_time <= now + _FUTURE_TOLERANCE


def aggregate_by_district(
    signals: list[PublicSignal] | tuple[PublicSignal, ...], now: datetime, window_hours: int = SIGNAL_WINDOW_HOURS
) -> tuple[DistrictSignals, ...]:
    """Count, latest time and source links per district, most reported first. A page counts once per district."""
    grouped: dict[str, dict[str, PublicSignal]] = {}
    for item in signals:
        if not in_window(item.event_time, now, window_hours):
            continue
        key = item.ubigeo or item.district
        page = grouped.setdefault(key, {})
        kept = page.get(item.source_url)
        if kept is None or item.event_time > kept.event_time:
            page[item.source_url] = item
    result = []
    for pages in grouped.values():
        members = sorted(pages.values(), key=lambda s: s.event_time, reverse=True)
        head = members[0]
        result.append(
            DistrictSignals(
                ubigeo=head.ubigeo,
                district=head.district,
                count=len(members),
                latest=head.event_time,
                sources=tuple(SignalSource(m.source_url, m.source_domain, m.event_type, m.event_time) for m in members),
                lon=head.lon,
                lat=head.lat,
                simulated=all(m.simulated for m in members),
            )
        )
    return tuple(sorted(result, key=lambda d: (-d.count, -d.latest.timestamp(), d.district)))


# --- the guardrail --------------------------------------------------------------------------------------------


def corroborate(evaluation: TierEvaluation, signal_count: int, min_signals: int = MIN_CORROBORATING_SIGNALS) -> TierEvaluation:
    """Add one reason to an ATTENTION evaluation when enough public pages report events nearby. Nothing else changes.

    The returned evaluation has the same tier and the same `triggered` tuple as the input for every input: unverified signals
    cannot create a tier, raise one, or lower one. Only an evaluation that is already ATTENTION gets the extra reason.
    """
    if evaluation.tier is not AlertTier.ATTENTION or signal_count < max(1, min_signals):
        return evaluation
    reason = (
        f"[ATTENTION] corroborated by {signal_count} public signals ({SIGNAL_LABEL_ES}); "
        "context only, the tier does not depend on them"
    )
    return replace(evaluation, reasons=evaluation.reasons + (reason,))

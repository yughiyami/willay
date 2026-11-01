"""Use cases for public signals: search the open web, extract events with a language model, geocode them deterministically.

Pipeline (see docs/SIGNALS.md):
  1. `build_queries`: hazard terms x the district names of our own basin settlements.
  2. A PublicSearchPort returns title, URL, published date and a short snippet of news and public pages of the last 72 hours.
  3. `extract_events`: a language model (Muse Spark structured output) reads those four fields and returns, per reported event,
     {event_type, place_name, district_ubigeo_guess, event_time, confidence, source_url}. Anything outside that contract is
     dropped, and the model cannot invent a link: `source_url` must be one of the URLs it was given.
  4. `signals_from_events`: the place name is matched, exactly after normalization, against OUR settlements and districts. An
     unmatched or ambiguous name is dropped. The stored place is our canonical name, never the text of the page.
  5. The raw text (titles, snippets, model answers) is discarded here. What the hub keeps per signal: event type, our place name,
     district, coordinates, event time, extractor confidence, and the page URL and domain.

Signals are unverified context. They never change a tier (see willay.domain.signals.corroborate) and never start a dispatch.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from willay.domain.entities import Settlement
from willay.domain.signals import (
    MIN_CORROBORATING_SIGNALS,
    SIGNAL_LABEL_ES,
    SIGNAL_WINDOW_HOURS,
    DistrictSignals,
    Gazetteer,
    GazetteerEntry,
    PublicSignal,
    SignalEventType,
    aggregate_by_district,
    in_window,
    normalize_place,
)
from willay.ports.agent import StructuredExtractionPort
from willay.ports.signals import PublicSearchPort, SearchResult, is_social_host, registered_host

SIGNALS_MODEL = "muse-spark-1.3"
MIN_CONFIDENCE = 0.5
BATCH_SIZE = 10  # results per model call, so one refresh has a bounded cost
DEFAULT_MAX_QUERIES = 8
DEFAULT_GROUP_SIZE = 5
HAZARD_TERMS = ("huaico", "deslizamiento", "desborde", "relave", "inundación")
EVENT_TYPE_VALUES = [t.value for t in SignalEventType]
DISCLAIMER_ES = (
    "Señales públicas no verificadas: noticias y páginas web de las últimas 72 horas, leídas por un modelo y ubicadas solo si "
    "el lugar coincide exactamente con nuestros centros poblados o distritos. Son contexto: no cambian el nivel de alerta, no "
    "envían nada y complementan, sin reemplazar, los avisos de SENAMHI, INDECI y ENFEN."
)
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "yclid", "msclkid")

_INSTRUCTIONS = (
    "You extract reports of recent natural-hazard events from web search results about Peru. The input is a JSON list of results, "
    "each with url, title, published and snippet. Return {\"events\": [...]}, one entry per event reported for one place. "
    "Rules: keep only events that the page says already happened in the last 72 hours in the Lima region or the Rimac river basin; "
    "skip forecasts, warnings, drills, anniversaries, opinion and older events. event_type is one of huaico, deslizamiento, "
    "desborde, relave, inundacion. place_name is the town, district or locality exactly as written on the page: a place only, never "
    "the name of a person, a handle, an address, a phone number or an organisation. district_ubigeo_guess is the 6-digit UBIGEO "
    "only if you are sure, otherwise null. event_time is ISO 8601 with time zone when the text gives it, otherwise null. "
    "confidence is between 0 and 1: how sure you are that this page reports this event at this place. source_url is copied "
    "exactly from the input. Never output personal data: no names of people, handles, profile links or phone numbers. "
    "If nothing qualifies return {\"events\": []}."
)

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string", "enum": EVENT_TYPE_VALUES},
                    "place_name": {"type": "string"},
                    "district_ubigeo_guess": {"type": ["string", "null"]},
                    "event_time": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "source_url": {"type": "string"},
                },
                "required": ["event_type", "place_name", "district_ubigeo_guess", "event_time", "confidence", "source_url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}


# --- our own place names --------------------------------------------------------------------------------


def _positions(coordinates: object) -> list[tuple[float, float]]:
    if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2 and all(isinstance(v, (int, float)) for v in coordinates[:2]):
        return [(float(coordinates[0]), float(coordinates[1]))]
    found: list[tuple[float, float]] = []
    if isinstance(coordinates, (list, tuple)):
        for part in coordinates:
            found.extend(_positions(part))
    return found


def _bbox_center(geometry: Mapping | None) -> tuple[float, float] | None:
    if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    points = _positions(geometry.get("coordinates"))
    if not points:
        return None
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return (min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0


def _is_named(settlement: Settlement) -> bool:
    name = settlement.name.strip()
    return bool(name) and name.lower() != "unnamed settlement"


def build_gazetteer(settlements: Sequence[Settlement], district_collection: Mapping | None) -> Gazetteer:
    """Settlements (ANA) and districts (polygons) as the only places a signal can be located at."""
    entries: list[GazetteerEntry] = []
    members: dict[str, list[Settlement]] = {}
    for s in settlements:
        if s.ubigeo:
            members.setdefault(s.ubigeo, []).append(s)
        if _is_named(s):
            entries.append(GazetteerEntry(s.name.strip(), s.ubigeo, s.district, s.lon, s.lat, "settlement"))

    def centroid(ubigeo: str) -> tuple[float, float] | None:
        group = members.get(ubigeo)
        if not group:
            return None
        return sum(s.lon for s in group) / len(group), sum(s.lat for s in group) / len(group)

    covered: set[str] = set()
    for feature in (district_collection or {}).get("features", []):
        props = feature.get("properties") or {}
        ubigeo, name = str(props.get("ubigeo") or ""), str(props.get("name") or "").strip()
        if not ubigeo or not name:
            continue
        position = _bbox_center(feature.get("geometry")) or centroid(ubigeo)
        if position is None:
            continue
        covered.add(ubigeo)
        entries.append(GazetteerEntry(name, ubigeo, name, position[0], position[1], "district"))
    for ubigeo, group in members.items():
        name = next((s.district for s in group if s.district), "")
        if ubigeo not in covered and name:
            lon, lat = centroid(ubigeo)  # type: ignore[misc]
            entries.append(GazetteerEntry(name, ubigeo, name, lon, lat, "district"))
    return Gazetteer(entries)


def build_queries(gazetteer: Gazetteer, max_queries: int = DEFAULT_MAX_QUERIES, group_size: int = DEFAULT_GROUP_SIZE) -> list[str]:
    """One query per group of basin districts: all five hazard terms OR-ed, then the places OR-ed."""
    places = [p.title() for p in gazetteer.query_places()]
    terms = " OR ".join(HAZARD_TERMS)
    size = max(1, group_size)
    groups = [places[i : i + size] for i in range(0, len(places), size)]
    return [f"{terms} {' OR '.join(group)} Perú" for group in groups[: max(0, max_queries)]]


# --- URLs --------------------------------------------------------------------------------------------------


def clean_url(url: str) -> str:
    """Page address without credentials, fragment or tracking parameters (a link we can show, with nothing personal in it)."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = f"{host}:{parts.port}" if parts.port else host
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith(_TRACKING_PARAMS)])
    return urlunsplit((parts.scheme, netloc, parts.path or "/", query, ""))


def _web_page(url: str) -> bool:
    host = registered_host(url)
    return bool(host) and not is_social_host(host)


def filter_results(results: Sequence[SearchResult], now: datetime) -> list[SearchResult]:
    """Public web pages only, once each, published inside the 72-hour window (an unknown date is kept for the model to judge)."""
    seen: set[str] = set()
    kept: list[SearchResult] = []
    for item in results:
        if not _web_page(item.url) or item.url in seen:
            continue
        if item.published is not None and not in_window(item.published, now):
            continue
        seen.add(item.url)
        kept.append(item)
    return kept


# --- extraction ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedEvent:
    """What the model may tell us about one event. No text, no author: this is the whole record."""

    event_type: SignalEventType
    place_name: str
    ubigeo_guess: str | None
    event_time: datetime
    confidence: float
    source_url: str


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_event(raw: object, published: Mapping[str, datetime | None], now: datetime) -> ExtractedEvent | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        event_type = SignalEventType(str(raw.get("event_type")))
    except ValueError:
        return None
    url = raw.get("source_url")
    if not isinstance(url, str) or url not in published:
        return None  # the model may only cite a page it was given
    place = raw.get("place_name")
    if not isinstance(place, str) or not place.strip():
        return None
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not MIN_CONFIDENCE <= float(confidence) <= 1.0:
        return None
    when = _parse_time(raw.get("event_time"))
    if when is None:
        when = published[url]
    if when is None or not in_window(when, now):
        return None
    guess = raw.get("district_ubigeo_guess")
    guess = guess.strip() if isinstance(guess, str) and guess.strip().isdigit() and len(guess.strip()) == 6 else None
    return ExtractedEvent(event_type, place.strip(), guess, when, float(confidence), url)


def extract_events(
    results: Sequence[SearchResult],
    extractor: StructuredExtractionPort,
    now: datetime,
    model: str = SIGNALS_MODEL,
    errors: list[str] | None = None,
) -> list[ExtractedEvent]:
    """Ask the model for events, in batches. With `errors` given, a failed batch is recorded and the others still run."""
    pages = [r for r in results if _web_page(r.url)]
    published = {r.url: r.published for r in pages}
    events: list[ExtractedEvent] = []
    for start in range(0, len(pages), BATCH_SIZE):
        batch = pages[start : start + BATCH_SIZE]
        payload = [
            {"url": r.url, "title": r.title, "published": r.published.isoformat() if r.published else None, "snippet": r.snippet}
            for r in batch
        ]
        try:
            answer = extractor.extract_json(
                instructions=_INSTRUCTIONS,
                content=[{"type": "input_text", "text": "Search results (JSON):\n" + json.dumps(payload, ensure_ascii=False)}],
                schema_name="public_signal_events",
                schema=EXTRACTION_SCHEMA,
                purpose="public_signals",
                model=model,
                max_output_tokens=3000,
            )
        except Exception as exc:
            if errors is None:
                raise
            errors.append(f"extraction failed: {_error_name(exc)}")
            continue
        raw_events = answer.get("events") if isinstance(answer, Mapping) else None
        if not isinstance(raw_events, list):
            continue
        for raw in raw_events:
            event = _parse_event(raw, published, now)
            if event is not None:
                events.append(event)
    return events


def _error_name(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" (HTTP {status})" if isinstance(status, int) else "")


# --- geocoding -----------------------------------------------------------------------------------------------------


def signals_from_events(events: Sequence[ExtractedEvent], gazetteer: Gazetteer) -> tuple[list[PublicSignal], int]:
    """Exact normalized geocoding against our places. Returns the signals and how many events were dropped as unmatched."""
    signals: list[PublicSignal] = []
    seen: set[tuple[str, str, SignalEventType]] = set()
    dropped = 0
    for event in events:
        if not _web_page(event.source_url):
            continue
        match = gazetteer.lookup(event.place_name, event.ubigeo_guess)
        if match is None:
            dropped += 1
            continue
        url = clean_url(event.source_url)
        key = (url, match.ubigeo or match.district, event.event_type)
        if key in seen:
            continue
        seen.add(key)
        signals.append(
            PublicSignal(
                event_type=event.event_type,
                place=match.place,
                ubigeo=match.ubigeo,
                district=match.district,
                lon=match.lon,
                lat=match.lat,
                event_time=event.event_time,
                confidence=event.confidence,
                source_url=url,
                source_domain=registered_host(url),
                level=match.level,
            )
        )
    return signals, dropped


# --- the hub ---------------------------------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


class PublicSignalsHub:
    """What the app currently knows: live signals (when enabled) or the labelled simulated fixture. Nothing else is kept."""

    def __init__(
        self,
        enabled: bool = False,
        clock: Callable[[], datetime] = _utcnow,
        simulated_loader: Callable[[datetime], list[PublicSignal]] | None = None,
        note: str = "",
    ) -> None:
        self.enabled = enabled
        self.note = note
        self._clock = clock
        self._loader = simulated_loader
        self._simulated = False
        self._live: dict[tuple[str, str, SignalEventType], PublicSignal] = {}
        self._refreshed_at: datetime | None = None
        self._last_report: dict | None = None
        self._lock = threading.Lock()

    # --- state ----------------------------------------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    @property
    def mode(self) -> str:
        if self.enabled:
            return "live"
        return "simulated" if self._simulated and self._loader is not None else "off"

    @property
    def simulation_available(self) -> bool:
        return self._loader is not None

    def set_simulated(self, on: bool) -> None:
        """Show the labelled offline fixture. Ignored while live signals are enabled: the two are never mixed."""
        with self._lock:
            self._simulated = bool(on)

    def replace(self, signals: Sequence[PublicSignal], refreshed_at: datetime) -> None:
        with self._lock:
            self._live = {}
        self.merge(signals, refreshed_at)

    def merge(self, signals: Sequence[PublicSignal], refreshed_at: datetime) -> None:
        with self._lock:
            for item in signals:
                self._live[(item.source_url, item.ubigeo or item.district, item.event_type)] = item
            self._refreshed_at = refreshed_at

    def record_report(self, report: "RefreshReport") -> None:
        with self._lock:
            self._last_report = report.to_dict()

    def seconds_since_refresh(self) -> float | None:
        with self._lock:
            return None if self._refreshed_at is None else (self._clock() - self._refreshed_at).total_seconds()

    # --- reads ----------------------------------------------------------------------------------------------------

    def _current(self) -> list[PublicSignal]:
        now = self._clock()
        mode = self.mode
        if mode == "live":
            with self._lock:
                items = list(self._live.values())
        elif mode == "simulated":
            items = list(self._loader(now)) if self._loader else []
        else:
            items = []
        return [s for s in items if in_window(s.event_time, now)]

    @staticmethod
    def _matches(item: PublicSignal, district: str) -> bool:
        wanted = district.strip()
        return item.ubigeo == wanted or normalize_place(item.district) == normalize_place(wanted)

    def signals(self, district: str | None = None) -> list[PublicSignal]:
        items = self._current()
        return [s for s in items if self._matches(s, district)] if district else items

    def districts(self, district: str | None = None) -> tuple[DistrictSignals, ...]:
        return aggregate_by_district(self.signals(district), self._clock())

    def count_for(self, ubigeos: set[str], districts: set[str]) -> int:
        """Distinct pages that report an event in any of these districts (by UBIGEO or normalized name)."""
        names = {normalize_place(d) for d in districts}
        pages = {
            s.source_url
            for s in self._current()
            if (s.ubigeo and s.ubigeo in ubigeos) or (names and normalize_place(s.district) in names)
        }
        return len(pages)

    def snapshot(self, district: str | None = None) -> dict:
        items = self.signals(district)
        aggregated = aggregate_by_district(items, self._clock())
        mode = self.mode
        return {
            "mode": mode,
            "enabled": self.enabled,
            "simulated": mode == "simulated",
            "simulation_available": self.simulation_available,
            "label": SIGNAL_LABEL_ES,
            "disclaimer": DISCLAIMER_ES,
            "window_hours": SIGNAL_WINDOW_HOURS,
            "min_corroborating_signals": MIN_CORROBORATING_SIGNALS,
            "refreshed_at": None if self._refreshed_at is None else _iso(self._refreshed_at),
            "note": self.note,
            "last_refresh": self._last_report,
            "districts": [
                {
                    "ubigeo": d.ubigeo,
                    "district": d.district,
                    "count": d.count,
                    "latest": _iso(d.latest),
                    "lon": d.lon,
                    "lat": d.lat,
                    "label": d.label,
                    "simulated": d.simulated,
                    "sources": [
                        {"url": s.url, "domain": s.domain, "event_type": s.event_type.value, "event_time": _iso(s.event_time)}
                        for s in d.sources
                    ],
                }
                for d in aggregated
            ],
            "signals": [
                {
                    "event_type": s.event_type.value,
                    "place": s.place,
                    "ubigeo": s.ubigeo,
                    "district": s.district,
                    "lon": s.lon,
                    "lat": s.lat,
                    "event_time": _iso(s.event_time),
                    "confidence": round(s.confidence, 2),
                    "source_url": s.source_url,
                    "source_domain": s.source_domain,
                    "level": s.level,
                    "simulated": s.simulated,
                }
                for s in sorted(items, key=lambda s: s.event_time, reverse=True)
            ],
        }


# --- reading signals for a site ----------------------------------------------------------------------------------------


def site_districts(ranked, settlements: Sequence[Settlement]) -> tuple[set[str], set[str]]:
    """UBIGEOs and district names that count as 'near' a site: its own district and those of the settlements its flow path reaches."""
    reached = {c.settlement_id for c in ranked.exposure.contributions} if ranked.exposure else set()
    near = [s for s in settlements if s.id in reached]
    ubigeos = {s.ubigeo for s in near if s.ubigeo}
    names = {s.district for s in near if s.district}
    if ranked.site.district:
        names.add(ranked.site.district)
    return ubigeos, names


def signal_count_for_site(hub: PublicSignalsHub | None, ranked, settlements: Sequence[Settlement]) -> int:
    """Distinct public pages reporting an event in the site's district or downstream of it (0 when signals are off)."""
    if hub is None or hub.mode == "off":
        return 0
    ubigeos, names = site_districts(ranked, settlements)
    return hub.count_for(ubigeos, names)


TOOL_NOTE = (
    "Unverified public reports found on the open web (news and public pages, last 72 hours). They are context for the operator: "
    "they cannot change an alert tier or start a dispatch, and they never replace the official SENAMHI, INDECI and ENFEN alerts."
)


def describe_signals(hub: PublicSignalsHub | None, district: str | None = None, max_districts: int = 10, max_sources: int = 5) -> dict:
    """The tool view of the hub: districts with counts and links, never any text."""
    if hub is None or hub.mode == "off":
        return {
            "available": False,
            "mode": "off",
            "label": SIGNAL_LABEL_ES,
            "districts": [],
            "note": "Public signals are not enabled (WILLAY_SIGNALS_ENABLED=0). Nothing was searched; do not infer any.",
        }
    snapshot = hub.snapshot(district)
    districts = [
        {
            "district": d["district"],
            "ubigeo": d["ubigeo"],
            "count": d["count"],
            "latest": d["latest"],
            "sources": [dict(source) for source in d["sources"][:max_sources]],
        }
        for d in snapshot["districts"][:max_districts]
    ]
    return {
        "available": True,
        "mode": snapshot["mode"],
        "simulated": snapshot["simulated"],
        "label": SIGNAL_LABEL_ES,
        "window_hours": SIGNAL_WINDOW_HOURS,
        "districts": districts,
        "total_pages": sum(d["count"] for d in snapshot["districts"]),
        "refreshed_at": snapshot["refreshed_at"],
        "note": TOOL_NOTE,
    }


# --- refresh ----------------------------------------------------------------------------------------------------------


@dataclass
class RefreshReport:
    queries: int = 0
    results_seen: int = 0
    events_extracted: int = 0
    signals_kept: int = 0
    dropped_unmatched: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "queries": self.queries,
            "results_seen": self.results_seen,
            "events_extracted": self.events_extracted,
            "signals_kept": self.signals_kept,
            "dropped_unmatched": self.dropped_unmatched,
            "errors": list(self.errors),
        }


def refresh_public_signals(
    search: PublicSearchPort,
    extractor: StructuredExtractionPort,
    gazetteer: Gazetteer,
    hub: PublicSignalsHub,
    *,
    now: datetime,
    max_queries: int = DEFAULT_MAX_QUERIES,
    group_size: int = DEFAULT_GROUP_SIZE,
    max_results: int = 5,
    model: str = SIGNALS_MODEL,
) -> RefreshReport:
    """Search -> extract -> geocode -> hub. Titles, snippets and model answers live only inside this call."""
    report = RefreshReport()
    since = now - timedelta(hours=SIGNAL_WINDOW_HOURS)
    queries = build_queries(gazetteer, max_queries, group_size)
    report.queries = len(queries)
    found: list[SearchResult] = []
    for query in queries:
        try:
            found.extend(search.search(query, since=since, max_results=max_results))
        except Exception as exc:
            report.errors.append(f"search failed: {_error_name(exc)}")
    results = filter_results(found, now)
    report.results_seen = len(results)
    signals: list[PublicSignal] = []
    if results:
        events = extract_events(results, extractor, now, model, errors=report.errors)
        report.events_extracted = len(events)
        signals, report.dropped_unmatched = signals_from_events(events, gazetteer)
        report.signals_kept = len(signals)
    hub.merge(signals, refreshed_at=now)  # also stamps an empty refresh, so the cooldown applies
    hub.record_report(report)
    return report

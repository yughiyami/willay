"""Approximate drinking-water intakes from OpenStreetMap (Overpass JSON). No official open layer exists."""

from __future__ import annotations

import json
from pathlib import Path

from willay.domain.entities import WaterIntake

NOTE = "approximate (OpenStreetMap, ODbL); verify with SEDAPAL before any use"
_NOT_INTAKE_TAGS = ("highway", "public_transport", "leisure", "railway")


def _location(element: dict) -> tuple[float, float] | None:
    if "lat" in element and "lon" in element:
        return float(element["lon"]), float(element["lat"])
    center = element.get("center")
    if center:
        return float(center["lon"]), float(center["lat"])
    return None


def _classify(tags: dict) -> str | None:
    if any(key in tags for key in _NOT_INTAKE_TAGS):
        return None
    if tags.get("man_made") == "water_works":
        return "water_works"
    name = str(tags.get("name", "")).casefold()
    if "bocatoma" in name and (tags.get("waterway") or tags.get("industrial") == "water"):
        return "weir" if tags.get("waterway") == "weir" else "intake"
    return None


def parse_osm_intakes(data: dict) -> list[WaterIntake]:
    out: list[WaterIntake] = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        kind = _classify(tags)
        location = _location(element)
        if kind is None or location is None:
            continue
        out.append(
            WaterIntake(
                id=f"osm-{element['type']}-{element['id']}",
                name=str(tags.get("name") or "Unnamed OSM water works"),
                lon=location[0],
                lat=location[1],
                operator=str(tags.get("operator") or tags.get("owner") or ""),
                kind=kind,
                approximate=True,
                note=NOTE,
            )
        )
    return out


def load_osm_intakes(path: Path) -> list[WaterIntake]:
    return parse_osm_intakes(json.loads(Path(path).read_text(encoding="utf-8")))

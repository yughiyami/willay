"""Wording of a proposed safe point (Spanish and English).

A proposed point is computed from the DEM (`willay.domain.safe_point`); it is NOT an official or surveyed
location, so every text that mentions one carries the validation label.
"""

from __future__ import annotations

from willay.domain.safe_point import SafePoint

PROPOSED_LABEL = {
    "es": "propuesto — validar con la municipalidad",
    "en": "proposed — validate with the municipality",
}
NO_SAFE_POINT = {
    "es": "Consulte a su municipalidad el punto seguro",
    "en": "Ask your municipality for the safe point",
}
_DIRECTIONS = {
    "es": ("al norte", "al noreste", "al este", "al sureste", "al sur", "al suroeste", "al oeste", "al noroeste"),
    "en": ("to the north", "to the northeast", "to the east", "to the southeast", "to the south", "to the southwest", "to the west", "to the northwest"),
}


def _distance_text(distance_m: float) -> str:
    if distance_m < 1000.0:
        return f"{max(50, int(round(distance_m / 50.0)) * 50)} m"
    return f"{distance_m / 1000.0:.1f} km"


def describe_direction(bearing_deg: float, language: str) -> str:
    return _DIRECTIONS[language][int(((bearing_deg % 360.0) + 22.5) // 45) % 8]


def describe_proposed_safe_point(point: SafePoint, language: str) -> str:
    """For example: "a 1.2 km al noreste (propuesto — validar con la municipalidad)"."""
    where = describe_direction(point.bearing_deg, language)
    distance = _distance_text(point.distance_m)
    place = f"a {distance} {where}" if language == "es" else f"{distance} {where}"
    return f"{place} ({PROPOSED_LABEL[language]})"

"""CHIRPS basin-mean daily rainfall and the ENFEN communique timeline."""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from willay.adapters.geodata.text import clean, fold, to_float
from willay.domain.entities import DailyRain, EnfenAlertStatus, EnfenBulletin, EnfenMagnitude

_MAGNITUDE_WORD = re.compile(r"\b(extraordinary|strong|moderate|weak|neutral)\b", re.IGNORECASE)
_MAGNITUDE = {
    "extraordinary": EnfenMagnitude.EXTRAORDINARY,
    "strong": EnfenMagnitude.STRONG,
    "moderate": EnfenMagnitude.MODERATE,
    "weak": EnfenMagnitude.WEAK,
    "neutral": EnfenMagnitude.NEUTRAL,
}


def parse_chirps_rows(rows: Iterable[Mapping[str, str]]) -> list[DailyRain]:
    out: list[DailyRain] = []
    for row in rows:
        try:
            when = date.fromisoformat(clean(row.get("date")))
        except ValueError:
            continue
        out.append(
            DailyRain(
                date=when,
                basin_mean_mm=to_float(row.get("basin_mean_mm")) or 0.0,
                basin_max_cell_mm=to_float(row.get("basin_max_cell_mm")) or 0.0,
                sierra_mean_mm=to_float(row.get("sierra_mean_mm")) or 0.0,
                bbox_mean_mm=to_float(row.get("bbox_mean_mm")) or 0.0,
            )
        )
    return sorted(out, key=lambda d: d.date)


def _status(text: str) -> EnfenAlertStatus:
    folded = fold(text)
    if folded.startswith("ALERTA"):
        return EnfenAlertStatus.ALERT
    if folded.startswith("VIGILANCIA"):
        return EnfenAlertStatus.WATCH
    return EnfenAlertStatus.NOT_ACTIVE


def parse_enfen_rows(rows: Iterable[Mapping[str, str]]) -> list[EnfenBulletin]:
    """Parse the curated ENFEN timeline.

    The forecast magnitude is the FIRST magnitude word in the curated text ("weak to moderate" is
    weak, "strong to moderate" is strong). It is NEUTRAL while the system is not active. A bulletin
    that names no magnitude inherits the previous one.
    """
    parsed: list[tuple[date, str, EnfenAlertStatus, str]] = []
    for row in rows:
        try:
            when = date.fromisoformat(clean(row.get("date")))
        except ValueError:
            continue
        parsed.append(
            (when, clean(row.get("number")), _status(clean(row.get("alert_status"))), clean(row.get("magnitude_forecast")))
        )
    parsed.sort(key=lambda item: item[0])

    bulletins: list[EnfenBulletin] = []
    previous = EnfenMagnitude.NEUTRAL
    for when, number, status, text in parsed:
        if status is EnfenAlertStatus.NOT_ACTIVE:
            magnitude = EnfenMagnitude.NEUTRAL
        else:
            match = _MAGNITUDE_WORD.search(text)
            magnitude = _MAGNITUDE[match.group(1).lower()] if match else previous
        previous = magnitude
        bulletins.append(
            EnfenBulletin(date=when, number=number, alert_status=status, magnitude=magnitude, source_text=text)
        )
    return bulletins


def load_chirps_csv(path: Path) -> list[DailyRain]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return parse_chirps_rows(csv.DictReader(fh))


def load_enfen_csv(path: Path) -> list[EnfenBulletin]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return parse_enfen_rows(csv.DictReader(fh))

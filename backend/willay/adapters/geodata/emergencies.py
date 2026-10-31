"""INDECI-SINPAD emergencies (district + report date; no coordinates)."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Collection, Iterable, Mapping

from willay.adapters.geodata.text import clean, fold, pad_ubigeo
from willay.domain.entities import Emergency

# Rain-driven hazard types used as backtest labels (accent-free, upper case).
# Excluded on purpose: DERRUMBE DE CERRO, ALUD, EROSION (not clearly rain-driven in this base), fires, quakes.
TARGET_HAZARDS: tuple[str, ...] = ("HUAYCO", "FLUJO DE DETRITOS", "DESLIZAMIENTO", "INUNDACION", "LLUVIA INTENSA")


def parse_indeci_rows(rows: Iterable[Mapping[str, str]]) -> list[Emergency]:
    out: list[Emergency] = []
    for row in rows:
        ubigeo = pad_ubigeo(row.get("ubigeo"))
        try:
            when = date.fromisoformat(clean(row.get("fecha"))[:10])
        except ValueError:
            continue
        if not ubigeo:
            continue
        out.append(
            Emergency(ubigeo=ubigeo, district=clean(row.get("dist")), date=when, hazard=fold(row.get("emergencia")))
        )
    return out


def filter_emergencies(
    emergencies: Iterable[Emergency], hazards: Collection[str], start: date, end: date
) -> list[Emergency]:
    """Emergencies of the given hazard types reported within [start, end] (inclusive)."""
    wanted = {fold(h) for h in hazards}
    return [e for e in emergencies if e.hazard in wanted and start <= e.date <= end]


def first_emergency_by_district(emergencies: Iterable[Emergency]) -> dict[str, date]:
    first: dict[str, date] = {}
    for e in emergencies:
        if e.ubigeo not in first or e.date < first[e.ubigeo]:
            first[e.ubigeo] = e.date
    return first


def load_indeci_csv(path: Path) -> list[Emergency]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return parse_indeci_rows(csv.DictReader(fh))

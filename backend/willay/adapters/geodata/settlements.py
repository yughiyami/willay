"""ANA census-2017 settlements. Population comes from `Pob_rev`; `Pob` is corrupted and never read."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping

from willay.adapters.geodata.text import clean, pad_ubigeo, to_float
from willay.domain.entities import Settlement


def _population(row: Mapping[str, str]) -> int | None:
    for key in ("pob_rev", "Pob_rev", "POB_REV"):
        value = to_float(row.get(key))
        if value is not None:
            return int(round(value))
    return None


def parse_settlement_rows(rows: Iterable[Mapping[str, str]]) -> list[Settlement]:
    out: list[Settlement] = []
    for row in rows:
        lon = to_float(row.get("lon", row.get("X_LONG")))
        lat = to_float(row.get("lat", row.get("Y_LAT")))
        population = _population(row)
        if lon is None or lat is None or population is None or population < 0:
            continue
        out.append(
            Settlement(
                id=f"ana-{clean(row.get('IDCCPP'))}",
                name=clean(row.get("NOMBCCPP")) or "Unnamed settlement",
                lon=lon,
                lat=lat,
                population=population,
                households=0,  # the source publishes no household count; not invented
                ubigeo=pad_ubigeo(row.get("UBIGEO")),
                district=clean(row.get("NOMDIST")),
            )
        )
    return out


def load_settlements_csv(path: Path) -> list[Settlement]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return parse_settlement_rows(csv.DictReader(fh))

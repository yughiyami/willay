"""INGEMMET tailings inventory (primary) and MINEM 2011 mining liabilities (secondary layer)."""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from willay.adapters.geodata.text import clean, fold, to_float
from willay.domain.entities import RiskLevel, SiteStatus

_STATUS = {"ABANDONADO": SiteStatus.ABANDONED, "CERRADO": SiteStatus.INACTIVE, "ACTIVO": SiteStatus.ACTIVE}
_RISK = {
    "MUY ALTO": RiskLevel.VERY_HIGH,
    "ALTO": RiskLevel.HIGH,
    "MEDIO": RiskLevel.MEDIUM,
    "BAJO": RiskLevel.LOW,
}
DEFAULT_DEPTH_M = 10.0


@dataclass(frozen=True)
class TailingsRecord:
    id: str
    name: str
    lon: float
    lat: float
    in_basin: bool
    district: str
    province: str
    status: SiteStatus
    declared_risk: RiskLevel
    volume_m3: float | None
    area_m2: float | None
    data_quality: tuple[str, ...] = ()


def parse_tailings_rows(rows: Iterable[Mapping[str, str]]) -> list[TailingsRecord]:
    """Parse INGEMMET 'Relave - inventario' rows.

    ESTADO/RIESGO are filled for only a few sites: a blank or unrecognised value becomes UNKNOWN
    with a data_quality flag, never a guess. A missing VOLUMEN is estimated from area x depth when
    the area is known (flagged); anything still missing is imputed later by `impute_missing_volumes`.
    """
    records: list[TailingsRecord] = []
    for row in rows:
        lon, lat = to_float(row.get("lon")), to_float(row.get("lat"))
        if lon is None or lat is None:
            continue
        flags: list[str] = []
        status = _STATUS.get(fold(row.get("ESTADO")), SiteStatus.UNKNOWN)
        if status is SiteStatus.UNKNOWN:
            flags.append("status_unknown")
        risk = _RISK.get(fold(row.get("RIESGO")), RiskLevel.UNKNOWN)
        if risk is RiskLevel.UNKNOWN:
            flags.append("risk_level_unknown")
        volume = to_float(row.get("VOLUMEN"))
        area = to_float(row.get("AREA_M2"))
        if volume is None and area is not None:
            depth = to_float(row.get("PROFUNDIDAD_M")) or DEFAULT_DEPTH_M
            volume = area * depth
            flags.append("volume_estimated_from_area")
        records.append(
            TailingsRecord(
                id=f"ingemmet-{clean(row.get('ID'))}",
                name=clean(row.get("NOMBRE_PAS")) or "Unnamed tailings",
                lon=lon,
                lat=lat,
                in_basin=clean(row.get("in_rimac_basin")) == "1",
                district=clean(row.get("DISTRITO")),
                province=clean(row.get("PROVINCIA")),
                status=status,
                declared_risk=risk,
                volume_m3=volume,
                area_m2=area,
                data_quality=tuple(flags),
            )
        )
    return records


def impute_missing_volumes(records: Sequence[TailingsRecord]) -> list[TailingsRecord]:
    """Fill still-missing volumes with the median of the known ones, flagged."""
    known = [r.volume_m3 for r in records if r.volume_m3 is not None]
    median = statistics.median(known) if known else 0.0
    return [
        r
        if r.volume_m3 is not None
        else replace(r, volume_m3=median, data_quality=r.data_quality + ("volume_imputed_median",))
        for r in records
    ]


def load_tailings_csv(path: Path) -> list[TailingsRecord]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return impute_missing_volumes(parse_tailings_rows(csv.DictReader(fh)))


@dataclass(frozen=True)
class PasivoRecord:
    id: str
    name: str
    type: str
    subtype: str
    lon: float
    lat: float
    in_basin: bool
    district: str
    inventory_year: int = 2011
    data_quality: tuple[str, ...] = ("minem_2011_inventory",)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "subtype": self.subtype,
            "lon": self.lon,
            "lat": self.lat,
            "in_basin": self.in_basin,
            "district": self.district,
            "inventory_year": self.inventory_year,
            "data_quality": list(self.data_quality),
        }


def parse_pasivo_rows(rows: Iterable[Mapping[str, str]]) -> list[PasivoRecord]:
    """MINEM mining liabilities (Oct 2011 inventory): a secondary, referential layer."""
    out: list[PasivoRecord] = []
    for row in rows:
        lon, lat = to_float(row.get("lon")), to_float(row.get("lat"))
        if lon is None or lat is None:
            continue
        out.append(
            PasivoRecord(
                id=f"minem-{clean(row.get('OBJECTID'))}",
                name=clean(row.get("NOMBREDM")) or "Unnamed liability",
                type=clean(row.get("TIPO")),
                subtype=clean(row.get("SUBTIPO")),
                lon=lon,
                lat=lat,
                in_basin=clean(row.get("in_rimac_basin")) == "1",
                district=clean(row.get("DISTRITO")),
            )
        )
    return out


def load_pasivos_csv(path: Path) -> list[PasivoRecord]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return parse_pasivo_rows(csv.DictReader(fh))

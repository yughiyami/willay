"""Use case: district-level backtest of static scores against INDECI emergencies.

Ground truth is district + date only (INDECI reports), so the backtest is at district level.
Every model (our static score, the CENEPRED scenario, a population-only baseline) is evaluated with
the same labels, the same metrics and the same PREPARATION gate, so differences come from the score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Sequence

from willay.application.run_backtest import BacktestDataset, DistrictRecord, run_backtest
from willay.domain.backtest_metrics import bootstrap_auc_ci, lead_time_stats, top_k_capture_rate
from willay.domain.district_score import DistrictFeatures, percentile_ranks, rank_average, score_districts
from willay.domain.entities import (
    AlertTier,
    DailyRain,
    EnfenBulletin,
    EnfenMagnitude,
    HazardContext,
    RainfallAnomaly,
)
from willay.domain.tiers import TierConfig, evaluate_tier


@dataclass(frozen=True)
class BacktestEvent:
    key: str
    label_start: date
    label_end: date
    signal_start: date  # first day for which ENFEN/CHIRPS signals are evaluated


STANDARD_EVENTS: Mapping[str, BacktestEvent] = {
    "2017": BacktestEvent("2017", date(2017, 1, 1), date(2017, 4, 30), signal_start=date(2017, 1, 1)),
    "2023": BacktestEvent("2023", date(2023, 3, 1), date(2023, 3, 31), signal_start=date(2023, 2, 1)),
}


@dataclass(frozen=True)
class DistrictRow:
    ubigeo: str
    name: str
    population: int = 0


@dataclass(frozen=True)
class ScenarioBaseline:
    """CENEPRED El Nino 2027 district scenario values and census population (comparison only)."""

    ubigeo: str
    name: str
    population: int
    flood_value: float | None
    mm_value: float | None


def build_model_scores(
    features: Sequence[DistrictFeatures], baselines: Sequence[ScenarioBaseline]
) -> dict[str, dict[str, float]]:
    """Scores per model. Our score uses only `features`; the baselines never feed into it."""
    models: dict[str, dict[str, float]] = {
        "willay_static": {s.ubigeo: s.value for s in score_districts(features)},
        "cenepred_baseline": {},
        "cenepred_mm_only": {},
        "cenepred_flood_only": {},
        "population_baseline": {},
        "ensemble_static_cenepred": {},
    }
    for b in baselines:
        present = [v for v in (b.flood_value, b.mm_value) if v is not None]
        if present:
            models["cenepred_baseline"][b.ubigeo] = sum(present) / len(present)
        if b.mm_value is not None:
            models["cenepred_mm_only"][b.ubigeo] = b.mm_value
        if b.flood_value is not None:
            models["cenepred_flood_only"][b.ubigeo] = b.flood_value
        models["population_baseline"][b.ubigeo] = float(b.population)
    # Fixed a-priori ensemble (equal weights, rank-average): our static score and the CENEPRED scenario mean.
    models["ensemble_static_cenepred"] = rank_average(models["willay_static"], models["cenepred_baseline"])
    return models


@dataclass(frozen=True)
class PreparationGate:
    """PREPARATION rule configuration for the backtest.

    `min_window_rain_mm` is a proxy for the 'seasonal rainfall anomaly forecast is above_normal'
    condition: no SENAMHI seasonal outlook exists for these events, and there is no CHIRPS
    climatology in the data folder, so an absolute accumulation threshold stands in for it.
    None disables the rainfall condition.
    """

    name: str
    min_enfen_magnitude: EnfenMagnitude
    rainfall_window_days: int = 30
    min_window_rain_mm: float | None = None
    district_rank_threshold: float = 0.5
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "min_enfen_magnitude": self.min_enfen_magnitude.value,
            "rainfall_window_days": self.rainfall_window_days,
            "min_window_rain_mm": self.min_window_rain_mm,
            "district_rank_threshold": self.district_rank_threshold,
            "description": self.description,
        }


def enfen_magnitude_on(bulletins: Sequence[EnfenBulletin], day: date) -> EnfenMagnitude:
    """Forecast magnitude from the latest communique published on or before `day`."""
    current = EnfenMagnitude.NEUTRAL
    for b in sorted(bulletins, key=lambda b: b.date):
        if b.date > day:
            break
        current = b.magnitude
    return current


def trailing_rain_mm(rain: Sequence[DailyRain], day: date, window_days: int) -> float:
    """Sum of the sierra-mean daily rainfall over the window ending on `day` (days that exist)."""
    first = day - timedelta(days=window_days - 1)
    return sum(r.sierra_mean_mm for r in rain if first <= r.date <= day)


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def first_preparation_dates(
    bulletins: Sequence[EnfenBulletin],
    rain: Sequence[DailyRain],
    district_ranks: Mapping[str, float],
    start: date,
    end: date,
    gate: PreparationGate,
) -> dict[str, date]:
    """First day each district satisfies the PREPARATION rule (existing `evaluate_tier`).

    The district rank in [0, 1] plays the role of the site static risk; each day only the
    information published up to that day is used.
    """
    config = TierConfig(
        min_enfen_magnitude=gate.min_enfen_magnitude,
        preparation_site_risk_threshold=gate.district_rank_threshold,
    )
    first: dict[str, date] = {}
    for day in _days(start, end):
        magnitude = enfen_magnitude_on(bulletins, day)
        rain_ok = gate.min_window_rain_mm is None or (
            trailing_rain_mm(rain, day, gate.rainfall_window_days) >= gate.min_window_rain_mm
        )
        context = HazardContext(
            enfen_magnitude=magnitude,
            seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL if rain_ok else RainfallAnomaly.NORMAL,
        )
        for ubigeo, rank in district_ranks.items():
            if ubigeo in first:
                continue
            if AlertTier.PREPARATION in evaluate_tier(context, rank, config).triggered:
                first[ubigeo] = day
    return first


def _slots(n: int, k_pct: int) -> int:
    return min(n, max(1, math.ceil(n * k_pct / 100.0 - 1e-9)))


def run_district_backtest(
    event: BacktestEvent,
    districts: Sequence[DistrictRow],
    models: Mapping[str, Mapping[str, float]],
    first_emergency: Mapping[str, date],
    bulletins: Sequence[EnfenBulletin],
    rain: Sequence[DailyRain],
    gates: Sequence[PreparationGate],
    primary_gate: str,
    top_k_pcts: tuple[int, ...] = (10, 20),
) -> dict:
    """Metrics per model (AUC, top-k capture) and lead times per model and gate."""
    gate_by_name = {g.name: g for g in gates}
    if primary_gate not in gate_by_name:
        raise ValueError(f"unknown primary gate '{primary_gate}'")

    ubigeos = [d.ubigeo for d in districts]
    n = len(ubigeos)
    emergency_flag = {u: u in first_emergency for u in ubigeos}
    n_positive = sum(emergency_flag.values())
    emergency_dates = {u: first_emergency[u] for u in ubigeos if u in first_emergency}

    result_models: dict[str, dict] = {}
    lead_time: dict[str, dict[str, dict]] = {g.name: {} for g in gates}
    gate_info: dict[str, dict] = {}
    eligible_ranks: dict[str, dict[str, float]] = {}

    for name, raw in models.items():
        values = [raw.get(u, 0.0) for u in ubigeos]
        ranks = dict(zip(ubigeos, [r if r is not None else 0.5 for r in percentile_ranks(values)]))
        eligible_ranks[name] = ranks

        first_prep = first_preparation_dates(
            bulletins, rain, ranks, event.signal_start, event.label_end, gate_by_name[primary_gate]
        )
        dataset = BacktestDataset(
            districts=tuple(
                DistrictRecord(
                    district=u,
                    score=float(raw.get(u, 0.0)),
                    emergency=emergency_flag[u],
                    first_preparation_alert=first_prep.get(u),
                    emergency_date=emergency_dates.get(u),
                )
                for u in ubigeos
            ),
            evacuation_events=(),
        )
        report = run_backtest(dataset, top_k_pcts=top_k_pcts)
        entry = report.to_dict()
        entry["auc_ci95"] = (
            list(bootstrap_auc_ci([float(raw.get(u, 0.0)) for u in ubigeos], [emergency_flag[u] for u in ubigeos]))
            if report.auc is not None
            else None
        )
        slots = {str(k): _slots(n, k) for k in top_k_pcts}
        entry["top_k_slots"] = slots
        entry["max_top_k_capture"] = {
            k: (min(1.0, s / n_positive) if n_positive else None) for k, s in slots.items()
        }
        entry["top_k_precision"] = {
            k: (v * n_positive / slots[k] if v is not None else None) for k, v in entry["top_k_capture"].items()
        }
        for drop in ("evacuation_false_alarm_rate", "lead_time_days", "synthetic", "n_districts", "n_emergencies"):
            entry.pop(drop, None)
        result_models[name] = entry

    for gate in gates:
        first_days: list[date] = []
        for name, ranks in eligible_ranks.items():
            first_prep = first_preparation_dates(
                bulletins, rain, ranks, event.signal_start, event.label_end, gate
            )
            stats = lead_time_stats(first_prep, emergency_dates)
            lead_time[gate.name][name] = stats.to_dict()
            first_days.extend(first_prep.values())
        gate_info[gate.name] = {
            **gate.to_dict(),
            "first_eligible_day": min(first_days).isoformat() if first_days else None,
            "primary": gate.name == primary_gate,
        }

    table = [
        {
            "ubigeo": d.ubigeo,
            "name": d.name,
            "population": d.population,
            "emergency": emergency_flag[d.ubigeo],
            "first_emergency": emergency_dates[d.ubigeo].isoformat() if d.ubigeo in emergency_dates else None,
            "scores": {name: models[name].get(d.ubigeo) for name in models},
        }
        for d in districts
    ]
    return {
        "event": event.key,
        "label_window": {"start": event.label_start.isoformat(), "end": event.label_end.isoformat()},
        "signal_start": event.signal_start.isoformat(),
        "n_districts": n,
        "n_positive": n_positive,
        "base_rate": n_positive / n if n else None,
        "primary_gate": primary_gate,
        "models": result_models,
        "lead_time": lead_time,
        "gates": gate_info,
        "districts": table,
    }


def recompute_top_k(event_result: Mapping, top_k_pcts: Sequence[int]) -> dict:
    """Copy of a stored event result with top-k metrics recomputed from its district table."""
    table = event_result["districts"]
    n = len(table)
    labels = [bool(d["emergency"]) for d in table]
    n_positive = sum(labels)
    models: dict[str, dict] = {}
    for name, stored in event_result["models"].items():
        scores = [float(d["scores"].get(name) or 0.0) for d in table]
        slots = {str(k): _slots(n, k) for k in top_k_pcts}
        capture = {str(k): (top_k_capture_rate(scores, labels, k) if n_positive else None) for k in top_k_pcts}
        models[name] = {
            **stored,
            "top_k_capture": capture,
            "top_k_slots": slots,
            "max_top_k_capture": {k: (min(1.0, s / n_positive) if n_positive else None) for k, s in slots.items()},
            "top_k_precision": {
                k: (v * n_positive / slots[k] if v is not None else None) for k, v in capture.items()
            },
        }
    return {**event_result, "models": models}

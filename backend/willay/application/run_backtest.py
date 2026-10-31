"""Use case: run the backtest metrics over a dataset of districts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from willay.domain.backtest_metrics import (
    LeadTimeStats,
    false_alarm_rate,
    lead_time_stats,
    roc_auc,
    top_k_capture_rate,
)


@dataclass(frozen=True)
class DistrictRecord:
    district: str
    score: float
    emergency: bool
    first_preparation_alert: date | None
    emergency_date: date | None


@dataclass(frozen=True)
class EvacuationEvent:
    district: str
    alerted: bool
    emergency_followed: bool


@dataclass(frozen=True)
class BacktestDataset:
    districts: tuple[DistrictRecord, ...]
    evacuation_events: tuple[EvacuationEvent, ...]
    synthetic: bool = False


@dataclass(frozen=True)
class BacktestReport:
    auc: float | None
    top_k_capture: dict[int, float | None]
    lead_time: LeadTimeStats
    evacuation_false_alarm_rate: float | None
    n_districts: int
    n_emergencies: int
    synthetic: bool

    def to_dict(self) -> dict:
        return {
            "auc": self.auc,
            "top_k_capture": {str(k): v for k, v in self.top_k_capture.items()},
            "lead_time_days": self.lead_time.to_dict(),
            "evacuation_false_alarm_rate": self.evacuation_false_alarm_rate,
            "n_districts": self.n_districts,
            "n_emergencies": self.n_emergencies,
            "synthetic": self.synthetic,
        }


def run_backtest(dataset: BacktestDataset, top_k_pcts: tuple[int, ...] = (10, 20)) -> BacktestReport:
    scores = [d.score for d in dataset.districts]
    labels = [d.emergency for d in dataset.districts]
    n_emergencies = sum(1 for y in labels if y)
    has_both_classes = 0 < n_emergencies < len(labels)

    first_prep = {d.district: d.first_preparation_alert for d in dataset.districts if d.first_preparation_alert}
    emergencies = {d.district: d.emergency_date for d in dataset.districts if d.emergency and d.emergency_date}

    return BacktestReport(
        auc=roc_auc(scores, labels) if has_both_classes else None,
        top_k_capture={
            k: top_k_capture_rate(scores, labels, k) if n_emergencies else None for k in top_k_pcts
        },
        lead_time=lead_time_stats(first_prep, emergencies),
        evacuation_false_alarm_rate=false_alarm_rate(
            [(e.alerted, e.emergency_followed) for e in dataset.evacuation_events]
        ),
        n_districts=len(dataset.districts),
        n_emergencies=n_emergencies,
        synthetic=dataset.synthetic,
    )

"""Backtest metrics as pure functions (no sklearn, no numpy)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence


def roc_auc(scores: Sequence[float], labels: Sequence[int | bool]) -> float:
    """ROC AUC via the Mann-Whitney U statistic; ties count as one half."""
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    pos = sum(1 for y in labels if y)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        raise ValueError("ROC AUC needs at least one positive and one negative label")

    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0  # ranks are 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1

    rank_sum_pos = sum(r for r, y in zip(ranks, labels) if y)
    u = rank_sum_pos - pos * (pos + 1) / 2.0
    return u / (pos * neg)


def top_k_capture_rate(scores: Sequence[float], labels: Sequence[int | bool], k_pct: float) -> float:
    """Share of observed emergencies that fall in the top k% scored districts.

    The group size is ceil(n * k / 100). Districts tied at the cutoff share the
    remaining slots uniformly, so the result is the expected capture under random tie-breaking.
    """
    if len(scores) != len(labels):
        raise ValueError("scores and labels must have the same length")
    if not 0 < k_pct <= 100:
        raise ValueError("k_pct must be within (0, 100]")
    total_pos = sum(1 for y in labels if y)
    if total_pos == 0:
        raise ValueError("capture rate needs at least one observed emergency")

    n = len(scores)
    slots = min(n, max(1, math.ceil(n * k_pct / 100.0 - 1e-9)))
    order = sorted(range(n), key=lambda i: -scores[i])

    captured = 0.0
    taken = 0
    i = 0
    while i < n and taken < slots:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        group = order[i : j + 1]
        group_pos = sum(1 for idx in group if labels[idx])
        if taken + len(group) <= slots:
            captured += group_pos
            taken += len(group)
        else:
            captured += group_pos * (slots - taken) / len(group)
            taken = slots
        i = j + 1
    return captured / total_pos


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile (same convention as numpy's default)."""
    if not values:
        raise ValueError("percentile of an empty sequence is undefined")
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class LeadTimeStats:
    values: tuple[int, ...]
    n: int
    median: float | None
    p25: float | None
    p75: float | None
    n_without_lead: int

    def to_dict(self) -> dict:
        return {
            "values": list(self.values),
            "n": self.n,
            "median": self.median,
            "p25": self.p25,
            "p75": self.p75,
            "n_without_lead": self.n_without_lead,
        }


def lead_time_stats(
    first_preparation_alert: Mapping[str, date], emergency_date: Mapping[str, date]
) -> LeadTimeStats:
    """Lead time in days between the first PREPARATION alert and the emergency, per district.

    Districts with an emergency but no preparation alert on or before the emergency date
    are counted in `n_without_lead` instead of being silently dropped.
    """
    leads: list[int] = []
    without = 0
    for district, emergency in emergency_date.items():
        alert = first_preparation_alert.get(district)
        if alert is None or alert > emergency:
            without += 1
            continue
        leads.append((emergency - alert).days)
    leads.sort()
    if not leads:
        return LeadTimeStats(values=(), n=0, median=None, p25=None, p75=None, n_without_lead=without)
    return LeadTimeStats(
        values=tuple(leads),
        n=len(leads),
        median=percentile(leads, 50),
        p25=percentile(leads, 25),
        p75=percentile(leads, 75),
        n_without_lead=without,
    )


def false_alarm_rate(events: Sequence[tuple[bool, bool]]) -> float | None:
    """Share of EVACUATION alerts that were not followed by an emergency.

    `events` are (alerted, emergency_followed) pairs. Returns None when no alert was issued.
    """
    alerts = [emergency for alerted, emergency in events if alerted]
    if not alerts:
        return None
    return sum(1 for emergency in alerts if not emergency) / len(alerts)

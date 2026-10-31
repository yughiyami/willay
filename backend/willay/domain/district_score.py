"""Static district score built only from our own physical inputs (no outcomes, no CENEPRED scenario).

Each feature is turned into a percentile rank in [0, 1] across the districts and the ranks are
combined with fixed, a-priori weights. Weights are NOT fitted to any label: they encode how much
we believe each input matters before looking at any emergency record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

FEATURE_NAMES = ("mm_high_share", "mean_slope_deg", "exposed_population", "tailings_upstream")

# Fixed a priori: susceptibility and exposure matter most; slope is partly inside the susceptibility
# model already; tailings upstream is a secondary amplifier and only exists in the mining belt.
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "mm_high_share": 0.35,
    "mean_slope_deg": 0.20,
    "exposed_population": 0.30,
    "tailings_upstream": 0.15,
}
NEUTRAL_SCORE = 0.5


@dataclass(frozen=True)
class DistrictFeatures:
    ubigeo: str
    name: str
    mm_high_share: float | None
    mean_slope_deg: float | None
    exposed_population: float | None
    tailings_upstream: float | None


@dataclass(frozen=True)
class DistrictScoreConfig:
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def __post_init__(self) -> None:
        if not self.weights:
            raise ValueError("at least one weight is required")
        for name, weight in self.weights.items():
            if name not in FEATURE_NAMES:
                raise ValueError(f"unknown feature '{name}'")
            if weight < 0:
                raise ValueError("weights must be >= 0")
        if sum(self.weights.values()) <= 0:
            raise ValueError("weights must not all be zero")


@dataclass(frozen=True)
class DistrictScore:
    ubigeo: str
    name: str
    value: float
    components: dict[str, float | None]
    missing: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "ubigeo": self.ubigeo,
            "name": self.name,
            "value": self.value,
            "components": dict(self.components),
            "missing": list(self.missing),
        }


def percentile_ranks(values: Sequence[float | None]) -> list[float | None]:
    """Average-rank percentile in [0, 1]; ties share the mean rank, missing values stay missing."""
    present = sorted((v, i) for i, v in enumerate(values) if v is not None)
    n = len(present)
    out: list[float | None] = [None] * len(values)
    if n == 0:
        return out
    if n == 1:
        out[present[0][1]] = 0.5
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][0] == present[i][0]:
            j += 1
        rank = (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            out[present[k][1]] = rank
        i = j + 1
    return out


def score_districts(
    districts: Sequence[DistrictFeatures], config: DistrictScoreConfig | None = None
) -> list[DistrictScore]:
    cfg = config or DistrictScoreConfig()
    ranks = {
        name: percentile_ranks([getattr(d, name) for d in districts]) for name in cfg.weights
    }
    scores: list[DistrictScore] = []
    for index, district in enumerate(districts):
        components = {name: ranks[name][index] for name in cfg.weights}
        usable = {name: rank for name, rank in components.items() if rank is not None}
        weight_sum = sum(cfg.weights[name] for name in usable)
        value = (
            sum(cfg.weights[name] * rank for name, rank in usable.items()) / weight_sum
            if weight_sum > 0
            else NEUTRAL_SCORE
        )
        scores.append(
            DistrictScore(
                ubigeo=district.ubigeo,
                name=district.name,
                value=value,
                components=components,
                missing=tuple(name for name, rank in components.items() if rank is None),
            )
        )
    return scores


def rank_average(*score_maps: Mapping[str, float]) -> dict[str, float]:
    """Equal-weight average of percentile ranks, over the districts present in every map.

    Only the order within each map matters (scales are not comparable), so the combination needs no fitted
    weights. A single common district gets the neutral rank 0.5.
    """
    if not score_maps:
        return {}
    common = sorted(set.intersection(*(set(m) for m in score_maps)))
    if not common:
        return {}
    per_map = [percentile_ranks([m[u] for u in common]) for m in score_maps]
    return {u: sum(ranks[i] for ranks in per_map) / len(per_map) for i, u in enumerate(common)}  # type: ignore[misc]

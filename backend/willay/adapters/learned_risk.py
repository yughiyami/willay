"""Learned district risk: a small L2-regularised logistic regression (numpy), trained on early seasons.

Unit of analysis: one row per (district, season). Label: the district had at least one INDECI rain-driven
emergency in the season window. Features: the static district inputs we already compute (INGEMMET susceptibility
share, mean slope, exposed population, tailings upstream). The data is small (78 districts, a few dozen features
at most), so a linear model is the honest choice; see docs/BACKTEST.md for the comparison and why no deep model.

Training seasons must come strictly before the test seasons. Imputation, scaling and the L2 strength are learned
from the training seasons only. This lives in adapters because it needs numpy; the domain stays stdlib-only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from willay.domain.backtest_metrics import roc_auc

FEATURE_NAMES = ("mm_high_share", "mean_slope_deg", "log_exposed_population", "log_tailings_upstream")
DEFAULT_L2_GRID = (0.1, 1.0, 10.0, 100.0)


class TemporalSplitError(ValueError):
    """Training seasons overlap or come after the test seasons."""


@dataclass(frozen=True)
class DistrictInputs:
    ubigeo: str
    mm_high_share: float | None
    mean_slope_deg: float | None
    exposed_population: float | None
    tailings_upstream: float | None

    def vector(self) -> list[float | None]:
        """Raw feature vector in FEATURE_NAMES order; None marks a missing value."""
        return [
            self.mm_high_share,
            self.mean_slope_deg,
            None if self.exposed_population is None else math.log1p(max(self.exposed_population, 0.0)),
            None if self.tailings_upstream is None else math.log1p(max(self.tailings_upstream, 0.0)),
        ]


@dataclass(frozen=True)
class SeasonRow:
    ubigeo: str
    year: int
    inputs: DistrictInputs
    label: int


def build_season_rows(
    districts: Sequence[DistrictInputs], positives_by_year: Mapping[int, set[str]], years: Sequence[int]
) -> list[SeasonRow]:
    """One row per district and year; `positives_by_year[year]` are the districts with an emergency that season."""
    rows: list[SeasonRow] = []
    for year in years:
        positive = positives_by_year.get(year, set())
        for district in districts:
            rows.append(SeasonRow(district.ubigeo, year, district, 1 if district.ubigeo in positive else 0))
    return rows


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))


def fit_logistic_l2(x: np.ndarray, y: np.ndarray, l2: float = 1.0, max_iter: int = 100) -> tuple[np.ndarray, float]:
    """Newton / IRLS for logistic regression with an L2 penalty on the weights (the intercept is not penalised)."""
    n, d = x.shape
    design = np.hstack([np.ones((n, 1)), x])
    beta = np.zeros(d + 1)
    penalty = np.diag([0.0] + [float(l2)] * d)
    for _ in range(max_iter):
        p = _sigmoid(design @ beta)
        gradient = design.T @ (p - y) + penalty @ beta
        weights = np.maximum(p * (1.0 - p), 1e-9)
        hessian = design.T @ (design * weights[:, None]) + penalty + 1e-9 * np.eye(d + 1)
        step = np.linalg.solve(hessian, gradient)
        beta = beta - step
        if float(np.max(np.abs(step))) < 1e-9:
            break
    return beta[1:], float(beta[0])


def brier_score(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels)


def _matrix(inputs: Sequence[DistrictInputs]) -> np.ndarray:
    return np.array([[np.nan if v is None else v for v in d.vector()] for d in inputs], dtype=float)


@dataclass(frozen=True)
class LearnedRiskModel:
    weights: tuple[float, ...]
    bias: float
    medians: Mapping[str, float]
    means: Mapping[str, float]
    stds: Mapping[str, float]
    l2: float
    train_years: tuple[int, ...]
    n_train_rows: int
    train_base_rate: float

    def _standardise(self, inputs: Sequence[DistrictInputs]) -> np.ndarray:
        raw = _matrix(inputs)
        for j, name in enumerate(FEATURE_NAMES):
            column = raw[:, j]
            column[np.isnan(column)] = self.medians[name]
            raw[:, j] = (column - self.means[name]) / self.stds[name]
        return raw

    def predict_proba(self, inputs: Sequence[DistrictInputs]) -> list[float]:
        if not inputs:
            return []
        z = self._standardise(inputs) @ np.array(self.weights) + self.bias
        return [float(p) for p in _sigmoid(z)]

    def to_dict(self) -> dict:
        return {
            "kind": "logistic_regression_l2",
            "features": list(FEATURE_NAMES),
            "coefficients": {name: w for name, w in zip(FEATURE_NAMES, self.weights)},
            "bias": self.bias,
            "medians": dict(self.medians),
            "means": dict(self.means),
            "stds": dict(self.stds),
            "l2": self.l2,
            "train_years": list(self.train_years),
            "n_train_rows": self.n_train_rows,
            "train_base_rate": self.train_base_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "LearnedRiskModel":
        return cls(
            weights=tuple(float(data["coefficients"][name]) for name in FEATURE_NAMES),
            bias=float(data["bias"]),
            medians=dict(data["medians"]),
            means=dict(data["means"]),
            stds=dict(data["stds"]),
            l2=float(data["l2"]),
            train_years=tuple(int(y) for y in data["train_years"]),
            n_train_rows=int(data["n_train_rows"]),
            train_base_rate=float(data["train_base_rate"]),
        )


def _fit_rows(rows: Sequence[SeasonRow], l2: float) -> LearnedRiskModel:
    inputs = [r.inputs for r in rows]
    y = np.array([r.label for r in rows], dtype=float)
    raw = _matrix(inputs)
    medians, means, stds = {}, {}, {}
    for j, name in enumerate(FEATURE_NAMES):
        column = raw[:, j]
        present = column[~np.isnan(column)]
        medians[name] = float(np.median(present)) if present.size else 0.0
        column = np.where(np.isnan(column), medians[name], column)
        means[name] = float(column.mean())
        std = float(column.std())
        stds[name] = std if std > 1e-12 else 1.0
        raw[:, j] = (column - means[name]) / stds[name]
    weights, bias = fit_logistic_l2(raw, y, l2)
    return LearnedRiskModel(
        weights=tuple(float(w) for w in weights),
        bias=bias,
        medians=medians,
        means=means,
        stds=stds,
        l2=float(l2),
        train_years=tuple(sorted({r.year for r in rows})),
        n_train_rows=len(rows),
        train_base_rate=float(y.mean()),
    )


def select_l2(train_rows: Sequence[SeasonRow], grid: Sequence[float] = DEFAULT_L2_GRID) -> tuple[float, dict[float, float | None]]:
    """Pick the L2 strength by leave-one-season-out AUC inside the training seasons (never touches test seasons)."""
    years = sorted({r.year for r in train_rows})
    report: dict[float, float | None] = {}
    for l2 in grid:
        aucs: list[float] = []
        for held_out in years:
            fit = [r for r in train_rows if r.year != held_out]
            test = [r for r in train_rows if r.year == held_out]
            labels = [r.label for r in test]
            if not fit or not any(labels) or all(labels):
                continue
            model = _fit_rows(fit, l2)
            aucs.append(roc_auc(model.predict_proba([r.inputs for r in test]), labels))
        report[l2] = sum(aucs) / len(aucs) if aucs else None
    scored = [(auc, -l2) for l2, auc in report.items() if auc is not None]
    best = max(scored)[1] * -1 if scored else grid[len(grid) // 2]
    return float(best), report


def train_model(
    rows: Sequence[SeasonRow],
    train_years: Sequence[int],
    test_years: Sequence[int],
    l2_grid: Sequence[float] = DEFAULT_L2_GRID,
) -> LearnedRiskModel:
    """Fit on `train_years` only. Refuses a split where a training season is not strictly before every test season."""
    if set(train_years) & set(test_years) or (test_years and max(train_years) >= min(test_years)):
        raise TemporalSplitError("training seasons must all come strictly before the test seasons")
    train_rows = [r for r in rows if r.year in set(train_years)]
    if not train_rows:
        raise ValueError("no training rows")
    l2, _ = select_l2(train_rows, l2_grid)
    return _fit_rows(train_rows, l2)

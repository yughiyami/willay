"""Learned district risk model: L2 logistic regression in numpy, trained on early seasons, tested later."""

import numpy as np
import pytest

from willay.adapters.learned_risk import (
    FEATURE_NAMES,
    DistrictInputs,
    LearnedRiskModel,
    SeasonRow,
    TemporalSplitError,
    brier_score,
    build_season_rows,
    fit_logistic_l2,
    select_l2,
    train_model,
)
from willay.domain.backtest_metrics import roc_auc


def make_data(n=400, seed=3):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 3))
    logits = 1.6 * x[:, 0] - 1.0 * x[:, 1] + 0.0 * x[:, 2] + 0.3
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logits))).astype(float)
    return x, y


def test_fit_recovers_signal_and_ignores_the_noise_feature():
    x, y = make_data()
    w, b = fit_logistic_l2(x, y, l2=1.0)
    assert w[0] > 0.8 and w[1] < -0.4
    assert abs(w[2]) < 0.35
    scores = x @ w + b
    assert roc_auc(scores.tolist(), y.astype(int).tolist()) > 0.75


def test_larger_l2_shrinks_the_weights():
    x, y = make_data()
    small, _ = fit_logistic_l2(x, y, l2=0.1)
    large, _ = fit_logistic_l2(x, y, l2=100.0)
    assert np.linalg.norm(large) < np.linalg.norm(small)


def test_intercept_is_not_penalised_so_the_mean_prediction_matches_the_base_rate():
    x, y = make_data()
    w, b = fit_logistic_l2(x, y, l2=50.0)
    probs = 1 / (1 + np.exp(-(x @ w + b)))
    assert probs.mean() == pytest.approx(y.mean(), abs=1e-6)


def test_fit_is_stable_on_perfectly_separable_data():
    x = np.array([[0.0], [1.0], [2.0], [3.0]])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    w, b = fit_logistic_l2(x, y, l2=1.0)
    assert np.all(np.isfinite(w)) and np.isfinite(b)


def districts():
    return [
        DistrictInputs("A", 0.9, 30.0, 50_000, 3),
        DistrictInputs("B", 0.1, 5.0, 100, 0),
        DistrictInputs("C", None, None, 0, 0),
        DistrictInputs("D", 0.6, 22.0, 9_000, 1),
    ]


def test_build_season_rows_has_one_row_per_district_and_year():
    labels = {2010: {"A", "D"}, 2011: {"A"}}
    rows = build_season_rows(districts(), labels, years=[2010, 2011])
    assert len(rows) == 8
    assert {(r.ubigeo, r.year): r.label for r in rows}[("A", 2010)] == 1
    assert {(r.ubigeo, r.year): r.label for r in rows}[("B", 2010)] == 0
    assert all(r.year in (2010, 2011) for r in rows)


def test_train_model_refuses_overlapping_train_and_test_seasons():
    rows = build_season_rows(districts(), {2010: {"A"}, 2017: {"A"}}, years=[2010, 2017])
    with pytest.raises(TemporalSplitError):
        train_model(rows, train_years=[2010, 2017], test_years=[2017])


def synthetic_rows():
    """Districts whose true emergency probability grows with susceptibility and slope."""
    rng = np.random.default_rng(11)
    inputs = []
    for i in range(60):
        susc = float(rng.uniform())
        slope = float(rng.uniform(0, 35))
        inputs.append(DistrictInputs(f"d{i:02d}", susc, slope, float(rng.integers(0, 40_000)), int(rng.integers(0, 3))))
    labels = {}
    for year in range(2003, 2020):
        labels[year] = {
            d.ubigeo for d in inputs if rng.uniform() < 1 / (1 + np.exp(-(4 * (d.mm_high_share - 0.5) + 0.08 * (d.mean_slope_deg - 17))))
        }
    return inputs, labels


def test_learned_model_beats_chance_on_later_seasons():
    inputs, labels = synthetic_rows()
    rows = build_season_rows(inputs, labels, years=list(range(2003, 2020)))
    model = train_model(rows, train_years=list(range(2003, 2017)), test_years=[2017])
    test_rows = [r for r in rows if r.year == 2017]
    probs = model.predict_proba([r.inputs for r in test_rows])
    assert roc_auc(probs, [r.label for r in test_rows]) > 0.7
    assert all(0.0 <= p <= 1.0 for p in probs)


def test_model_uses_only_training_statistics_for_imputation_and_scaling():
    inputs, labels = synthetic_rows()
    rows = build_season_rows(inputs, labels, years=list(range(2003, 2020)))
    model = train_model(rows, train_years=list(range(2003, 2017)), test_years=[2017])
    train_slopes = sorted({r.inputs.mean_slope_deg for r in rows if r.year < 2017})
    assert model.medians["mean_slope_deg"] == pytest.approx(float(np.median(train_slopes)))
    missing = DistrictInputs("zz", None, None, 0, 0)
    assert 0.0 <= model.predict_proba([missing])[0] <= 1.0


def test_serialisation_round_trip_gives_identical_predictions():
    inputs, labels = synthetic_rows()
    rows = build_season_rows(inputs, labels, years=list(range(2003, 2020)))
    model = train_model(rows, train_years=list(range(2003, 2017)), test_years=[2017])
    clone = LearnedRiskModel.from_dict(model.to_dict())
    assert clone.predict_proba(inputs) == pytest.approx(model.predict_proba(inputs))
    assert set(model.to_dict()["coefficients"]) == set(FEATURE_NAMES)


def test_select_l2_is_deterministic_and_comes_from_the_grid():
    inputs, labels = synthetic_rows()
    rows = [r for r in build_season_rows(inputs, labels, years=list(range(2003, 2017)))]
    grid = (0.1, 1.0, 10.0)
    first, report = select_l2(rows, grid)
    second, _ = select_l2(rows, grid)
    assert first == second and first in grid
    assert set(report) == set(grid)


def test_brier_score_perfect_and_worst():
    assert brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert brier_score([0.0, 1.0], [1, 0]) == 1.0


def test_season_row_is_a_plain_dataclass_with_label_zero_or_one():
    row = SeasonRow("A", 2010, DistrictInputs("A", 0.5, 10.0, 10, 0), 1)
    assert row.label in (0, 1)

from datetime import date

import pytest

from willay.application.run_backtest import (
    BacktestDataset,
    DistrictRecord,
    EvacuationEvent,
    run_backtest,
)


def dataset() -> BacktestDataset:
    districts = (
        DistrictRecord("A", 0.9, True, date(2026, 1, 1), date(2026, 2, 1)),
        DistrictRecord("B", 0.8, False, None, None),
        DistrictRecord("C", 0.4, True, None, date(2026, 3, 1)),
        DistrictRecord("D", 0.2, False, None, None),
        DistrictRecord("E", 0.1, False, None, None),
    )
    events = (
        EvacuationEvent("A", True, True),
        EvacuationEvent("B", True, False),
        EvacuationEvent("C", False, True),
    )
    return BacktestDataset(districts=districts, evacuation_events=events, synthetic=True)


def test_report_contains_all_metrics():
    report = run_backtest(dataset(), top_k_pcts=(20, 40))
    # positives A(0.9) and C(0.4); negatives B(0.8), D, E -> pairs won: A>3, C>D,E = 5 of 6
    assert report.auc == pytest.approx(5 / 6)
    assert report.top_k_capture == {20: 0.5, 40: 0.5}
    assert report.lead_time.values == (31,)
    assert report.lead_time.n_without_lead == 1
    assert report.evacuation_false_alarm_rate == 0.5
    assert report.n_districts == 5
    assert report.n_emergencies == 2


def test_report_flags_synthetic_data():
    assert run_backtest(dataset()).synthetic is True


def test_report_to_dict_is_json_friendly():
    data = run_backtest(dataset(), top_k_pcts=(20,)).to_dict()
    assert data["auc"] == pytest.approx(5 / 6)
    assert data["top_k_capture"] == {"20": 0.5}
    assert data["lead_time_days"]["median"] == 31
    assert data["synthetic"] is True


def test_report_without_positive_class_degrades_gracefully():
    only_negatives = BacktestDataset(
        districts=(DistrictRecord("A", 0.5, False, None, None), DistrictRecord("B", 0.1, False, None, None)),
        evacuation_events=(),
        synthetic=True,
    )
    report = run_backtest(only_negatives)
    assert report.auc is None
    assert report.top_k_capture == {10: None, 20: None}
    assert report.evacuation_false_alarm_rate is None

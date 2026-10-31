from datetime import date

import pytest

from willay.domain.backtest_metrics import (
    false_alarm_rate,
    lead_time_stats,
    percentile,
    roc_auc,
    top_k_capture_rate,
)


# --- ROC AUC (Mann-Whitney) -------------------------------------------------


def test_auc_perfect_separation():
    assert roc_auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0


def test_auc_perfectly_inverted():
    assert roc_auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0


def test_auc_all_ties_is_half():
    assert roc_auc([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == 0.5


def test_auc_known_value():
    # positives: 0.8, 0.4 ; negatives: 0.6, 0.2 -> pairs: (0.8>0.6)(0.8>0.2)(0.4<0.6)(0.4>0.2) = 3/4
    assert roc_auc([0.8, 0.6, 0.4, 0.2], [1, 0, 1, 0]) == pytest.approx(0.75)


def test_auc_counts_ties_as_half():
    # pos 0.5 vs neg 0.5 tie => 0.5 ; pos 0.5 vs neg 0.1 => 1 ; pos 0.9 vs both => 2 ; total 3.5/4
    assert roc_auc([0.5, 0.5, 0.9, 0.1], [1, 0, 1, 0]) == pytest.approx(0.875)


def test_auc_accepts_bool_labels():
    assert roc_auc([0.9, 0.1], [True, False]) == 1.0


def test_auc_requires_both_classes():
    with pytest.raises(ValueError):
        roc_auc([0.1, 0.2], [1, 1])
    with pytest.raises(ValueError):
        roc_auc([0.1, 0.2], [0, 0])


def test_auc_length_mismatch():
    with pytest.raises(ValueError):
        roc_auc([0.1], [1, 0])


# --- top-k% capture ---------------------------------------------------------


def test_top_k_capture_all_emergencies_in_top():
    scores = [0.9, 0.8, 0.1, 0.05, 0.02, 0.01, 0.0, 0.0, 0.0, 0.0]
    labels = [1, 1, 0, 0, 0, 0, 0, 0, 0, 0]
    assert top_k_capture_rate(scores, labels, 20) == 1.0


def test_top_k_capture_partial():
    scores = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]
    labels = [1, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    assert top_k_capture_rate(scores, labels, 20) == 0.5


def test_top_k_uses_ceil_for_group_size():
    scores = [0.9, 0.8, 0.7]
    labels = [0, 1, 0]
    # 10% of 3 districts -> ceil(0.3) = 1 district (the top one) -> misses the emergency
    assert top_k_capture_rate(scores, labels, 10) == 0.0


def test_top_k_handles_boundary_ties_in_expectation():
    scores = [0.5, 0.5, 0.5, 0.5]
    labels = [1, 0, 0, 0]
    # top 50% = 2 slots shared uniformly among 4 tied districts -> expected capture 0.5
    assert top_k_capture_rate(scores, labels, 50) == pytest.approx(0.5)


def test_top_k_validates_inputs():
    with pytest.raises(ValueError):
        top_k_capture_rate([0.1], [0], 10)  # no emergencies
    with pytest.raises(ValueError):
        top_k_capture_rate([0.1, 0.2], [1, 0], 0)
    with pytest.raises(ValueError):
        top_k_capture_rate([0.1, 0.2], [1, 0], 101)


# --- percentile / lead time -------------------------------------------------


def test_percentile_linear_interpolation():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 25) == 1.75
    assert percentile([1, 2, 3, 4], 75) == 3.25
    assert percentile([7], 25) == 7


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_lead_time_stats_basic():
    first_prep = {"A": date(2026, 1, 1), "B": date(2026, 1, 1), "C": date(2026, 2, 1)}
    emergency = {"A": date(2026, 2, 1), "B": date(2026, 3, 3), "C": date(2026, 2, 11)}
    stats = lead_time_stats(first_prep, emergency)
    assert stats.values == (10, 31, 61)
    assert stats.n == 3
    assert stats.median == 31
    assert stats.p25 == pytest.approx(20.5)
    assert stats.p75 == pytest.approx(46.0)
    assert stats.n_without_lead == 0


def test_lead_time_ignores_districts_missing_either_date():
    stats = lead_time_stats({"A": date(2026, 1, 1)}, {"A": date(2026, 1, 11), "B": date(2026, 1, 20)})
    assert stats.values == (10,)
    # B had an emergency but never had a preparation alert
    assert stats.n_without_lead == 1


def test_lead_time_counts_late_alerts_as_without_lead():
    stats = lead_time_stats({"A": date(2026, 2, 5)}, {"A": date(2026, 2, 1)})
    assert stats.values == ()
    assert stats.n == 0
    assert stats.n_without_lead == 1
    assert stats.median is None and stats.p25 is None and stats.p75 is None


def test_lead_time_same_day_is_zero_lead():
    stats = lead_time_stats({"A": date(2026, 2, 1)}, {"A": date(2026, 2, 1)})
    assert stats.values == (0,)


# --- false alarm rate -------------------------------------------------------


def test_false_alarm_rate_is_share_of_alerts_without_emergency():
    # (alerted, emergency_followed): 4 alerts, 1 followed by an emergency -> 3/4 false alarms
    events = [(True, True), (True, False), (True, False), (True, False), (False, True), (False, False)]
    assert false_alarm_rate(events) == 0.75


def test_false_alarm_rate_zero_when_all_alerts_verified():
    assert false_alarm_rate([(True, True), (True, True)]) == 0.0


def test_false_alarm_rate_undefined_without_alerts():
    assert false_alarm_rate([(False, True), (False, False)]) is None
    assert false_alarm_rate([]) is None

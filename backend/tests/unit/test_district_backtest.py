from datetime import date, timedelta

import pytest

from willay.application.district_backtest import (
    BacktestEvent,
    DistrictRow,
    PreparationGate,
    enfen_magnitude_on,
    first_preparation_dates,
    run_district_backtest,
    trailing_rain_mm,
)
from willay.domain.entities import DailyRain, EnfenAlertStatus, EnfenBulletin, EnfenMagnitude


def bulletin(day, magnitude, status=EnfenAlertStatus.ALERT, number="1"):
    return EnfenBulletin(date=day, number=number, alert_status=status, magnitude=magnitude)


def rain_series(start: date, daily_mm: list[float]) -> list[DailyRain]:
    return [
        DailyRain(date=start + timedelta(days=i), basin_mean_mm=mm, basin_max_cell_mm=mm, sierra_mean_mm=mm, bbox_mean_mm=mm)
        for i, mm in enumerate(daily_mm)
    ]


D = date  # shorthand


# --- ENFEN and CHIRPS signals ---------------------------------------------------


def test_magnitude_is_the_latest_bulletin_on_or_before_the_day_never_a_future_one():
    bulletins = [bulletin(D(2017, 1, 10), EnfenMagnitude.WEAK), bulletin(D(2017, 3, 1), EnfenMagnitude.MODERATE)]
    assert enfen_magnitude_on(bulletins, D(2017, 1, 9)) is EnfenMagnitude.NEUTRAL
    assert enfen_magnitude_on(bulletins, D(2017, 1, 10)) is EnfenMagnitude.WEAK
    assert enfen_magnitude_on(bulletins, D(2017, 2, 28)) is EnfenMagnitude.WEAK
    assert enfen_magnitude_on(bulletins, D(2017, 3, 1)) is EnfenMagnitude.MODERATE


def test_trailing_rain_sums_the_window_ending_on_the_day_and_ignores_later_days():
    rain = rain_series(D(2017, 1, 1), [1.0] * 10)
    assert trailing_rain_mm(rain, D(2017, 1, 5), 3) == pytest.approx(3.0)
    assert trailing_rain_mm(rain, D(2017, 1, 2), 5) == pytest.approx(2.0)  # only the days that exist
    assert trailing_rain_mm(rain, D(2016, 12, 31), 5) == 0.0


# --- first PREPARATION-eligible date --------------------------------------------


def gate(**overrides):
    base = dict(
        name="g",
        min_enfen_magnitude=EnfenMagnitude.WEAK,
        rainfall_window_days=3,
        min_window_rain_mm=None,
        district_rank_threshold=0.5,
    )
    base.update(overrides)
    return PreparationGate(**base)


def test_first_preparation_date_needs_enfen_magnitude_and_a_high_enough_district_rank():
    bulletins = [bulletin(D(2017, 1, 10), EnfenMagnitude.WEAK)]
    rain = rain_series(D(2017, 1, 1), [0.0] * 30)
    ranks = {"hi": 0.9, "lo": 0.1}
    first = first_preparation_dates(bulletins, rain, ranks, D(2017, 1, 1), D(2017, 1, 30), gate())
    assert first == {"hi": D(2017, 1, 10)}


def test_the_rain_gate_delays_eligibility_until_the_accumulation_is_reached():
    bulletins = [bulletin(D(2017, 1, 1), EnfenMagnitude.WEAK)]
    rain = rain_series(D(2017, 1, 1), [0, 0, 0, 0, 10, 10, 10, 0, 0, 0])
    first = first_preparation_dates(
        bulletins, rain, {"d": 1.0}, D(2017, 1, 1), D(2017, 1, 10), gate(min_window_rain_mm=20.0)
    )
    assert first == {"d": D(2017, 1, 6)}  # 10 + 10 within the 3-day window ending Jan 6


def test_strict_magnitude_never_fires_when_enfen_stays_below_strong():
    bulletins = [bulletin(D(2017, 1, 1), EnfenMagnitude.MODERATE)]
    rain = rain_series(D(2017, 1, 1), [50.0] * 20)
    strict = gate(min_enfen_magnitude=EnfenMagnitude.STRONG)
    assert first_preparation_dates(bulletins, rain, {"d": 1.0}, D(2017, 1, 1), D(2017, 1, 20), strict) == {}


def test_eligibility_uses_only_information_available_on_each_day():
    # The strong bulletin comes on Feb 1: January days must not be eligible under a STRONG gate.
    bulletins = [bulletin(D(2017, 1, 1), EnfenMagnitude.WEAK), bulletin(D(2017, 2, 1), EnfenMagnitude.STRONG)]
    rain = rain_series(D(2017, 1, 1), [10.0] * 40)
    strict = gate(min_enfen_magnitude=EnfenMagnitude.STRONG)
    first = first_preparation_dates(bulletins, rain, {"d": 1.0}, D(2017, 1, 1), D(2017, 2, 9), strict)
    assert first == {"d": D(2017, 2, 1)}


# --- end-to-end backtest ----------------------------------------------------------


EVENT = BacktestEvent(key="2017", label_start=D(2017, 1, 1), label_end=D(2017, 4, 30), signal_start=D(2017, 1, 1))


def rows():
    return [
        DistrictRow("1", "A", population=100),
        DistrictRow("2", "B", population=900),
        DistrictRow("3", "C", population=500),
        DistrictRow("4", "D", population=300),
        DistrictRow("5", "E", population=50),
    ]


def test_backtest_reports_each_model_with_auc_top_k_and_counts():
    first_emergency = {"1": D(2017, 3, 10), "2": D(2017, 3, 12)}
    models = {
        "willay_static": {"1": 0.9, "2": 0.8, "3": 0.3, "4": 0.2, "5": 0.1},  # perfect
        "population_baseline": {"1": 100, "2": 900, "3": 500, "4": 300, "5": 50},
    }
    result = run_district_backtest(
        EVENT,
        rows(),
        models,
        first_emergency,
        bulletins=[bulletin(D(2017, 2, 1), EnfenMagnitude.WEAK)],
        rain=rain_series(D(2017, 1, 1), [5.0] * 120),
        gates=[gate(name="primary")],
        primary_gate="primary",
        top_k_pcts=(20, 40),
    )
    assert result["event"] == "2017"
    assert result["n_districts"] == 5
    assert result["n_positive"] == 2
    assert result["base_rate"] == pytest.approx(0.4)
    willay = result["models"]["willay_static"]
    assert willay["auc"] == 1.0
    assert willay["top_k_capture"]["20"] == pytest.approx(0.5)  # 1 slot of 2 emergencies
    assert willay["top_k_capture"]["40"] == 1.0
    assert willay["max_top_k_capture"]["20"] == pytest.approx(0.5)
    assert result["models"]["population_baseline"]["auc"] == pytest.approx(5 / 6)


def test_lead_time_is_measured_from_the_first_eligible_day_to_the_first_emergency():
    first_emergency = {"1": D(2017, 3, 10), "2": D(2017, 1, 20)}
    models = {"willay_static": {"1": 0.9, "2": 0.8, "3": 0.3, "4": 0.2, "5": 0.1}}
    result = run_district_backtest(
        EVENT,
        rows(),
        models,
        first_emergency,
        bulletins=[bulletin(D(2017, 2, 1), EnfenMagnitude.WEAK)],
        rain=rain_series(D(2017, 1, 1), [5.0] * 120),
        gates=[gate(name="primary")],
        primary_gate="primary",
    )
    lead = result["lead_time"]["primary"]["willay_static"]
    assert lead["values"] == [38]  # Feb 1 -> Mar 10
    assert lead["n_without_lead"] == 1  # district 2 had its emergency before the first eligible day
    assert result["gates"]["primary"]["first_eligible_day"] == "2017-02-01"


def test_ranks_not_raw_scores_drive_the_gate_so_baselines_with_other_scales_are_comparable():
    first_emergency = {"5": D(2017, 3, 10)}
    models = {"population_baseline": {"1": 100, "2": 900, "3": 500, "4": 300, "5": 50}}
    result = run_district_backtest(
        EVENT,
        rows(),
        models,
        first_emergency,
        bulletins=[bulletin(D(2017, 2, 1), EnfenMagnitude.WEAK)],
        rain=rain_series(D(2017, 1, 1), [5.0] * 120),
        gates=[gate(name="primary")],
        primary_gate="primary",
    )
    # district 5 has the lowest population rank: never flagged, so no lead time
    assert result["lead_time"]["primary"]["population_baseline"]["n"] == 0
    assert result["lead_time"]["primary"]["population_baseline"]["n_without_lead"] == 1


def test_no_emergencies_gives_null_metrics_instead_of_failing():
    result = run_district_backtest(
        EVENT,
        rows(),
        {"willay_static": {"1": 0.5, "2": 0.4, "3": 0.3, "4": 0.2, "5": 0.1}},
        {},
        bulletins=[],
        rain=[],
        gates=[gate(name="primary")],
        primary_gate="primary",
    )
    assert result["n_positive"] == 0
    assert result["models"]["willay_static"]["auc"] is None
    assert result["models"]["willay_static"]["top_k_capture"]["20"] is None


def test_district_table_lists_scores_labels_and_dates():
    result = run_district_backtest(
        EVENT,
        rows(),
        {"willay_static": {"1": 0.9, "2": 0.8, "3": 0.3, "4": 0.2, "5": 0.1}},
        {"1": D(2017, 3, 10)},
        bulletins=[],
        rain=[],
        gates=[gate(name="primary")],
        primary_gate="primary",
    )
    first = next(d for d in result["districts"] if d["ubigeo"] == "1")
    assert first["emergency"] is True
    assert first["first_emergency"] == "2017-03-10"
    assert first["scores"]["willay_static"] == 0.9
    other = next(d for d in result["districts"] if d["ubigeo"] == "5")
    assert other["emergency"] is False and other["first_emergency"] is None

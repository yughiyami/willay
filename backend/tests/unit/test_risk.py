import math

import pytest

from willay.domain.entities import (
    EnfenMagnitude,
    HazardContext,
    RainfallAnomaly,
    RiskLevel,
    SenamhiLevel,
    SiteStatus,
    TailingsSite,
)
from willay.domain.risk import (
    NEUTRAL_DECLARED_RISK_FACTOR,
    NEUTRAL_STATUS_FACTOR,
    RiskConfig,
    compute_risk_score,
    declared_risk_factor,
    distance_to_river_factor,
    normalize_log_volume,
    status_factor,
    tailings_vulnerability,
    trigger_factor,
)


def make_site(**overrides) -> TailingsSite:
    base = dict(
        id="t1",
        name="Fake Site",
        lon=-76.5,
        lat=-11.8,
        volume_m3=100_000.0,
        status=SiteStatus.ACTIVE,
        declared_risk=RiskLevel.MEDIUM,
        distance_to_river_m=300.0,
        susceptibility=0.5,
        synthetic=True,
    )
    base.update(overrides)
    return TailingsSite(**base)


# --- volume ----------------------------------------------------------------


def test_volume_normalization_is_monotonic_and_bounded():
    cfg = RiskConfig()
    small = normalize_log_volume(1_000, cfg)
    mid = normalize_log_volume(100_000, cfg)
    big = normalize_log_volume(10_000_000, cfg)
    assert 0.0 <= small < mid < big <= 1.0


def test_volume_normalization_clamps_extremes():
    cfg = RiskConfig()
    assert normalize_log_volume(0, cfg) == 0.0
    assert normalize_log_volume(1e15, cfg) == 1.0


def test_volume_normalization_uses_log_scale_midpoint():
    cfg = RiskConfig(volume_min_m3=1e2, volume_max_m3=1e6)
    assert normalize_log_volume(1e4, cfg) == pytest.approx(0.5)


# --- categorical factors ---------------------------------------------------


def test_abandoned_is_worst_status():
    assert status_factor(SiteStatus.ABANDONED) > status_factor(SiteStatus.INACTIVE)
    assert status_factor(SiteStatus.INACTIVE) > status_factor(SiteStatus.ACTIVE)
    assert status_factor(SiteStatus.ABANDONED) == 1.0


def test_declared_risk_factor_is_ordered_and_bounded():
    values = [declared_risk_factor(r) for r in RiskLevel if r is not RiskLevel.UNKNOWN]
    assert values == sorted(values)
    assert values[-1] == 1.0
    assert values[0] > 0.0


def test_closer_to_river_is_worse():
    cfg = RiskConfig()
    assert distance_to_river_factor(0, cfg) == 1.0
    assert distance_to_river_factor(100, cfg) > distance_to_river_factor(1000, cfg)
    assert 0.0 <= distance_to_river_factor(50_000, cfg) < 0.01


# --- tailings vulnerability -------------------------------------------------


def test_tailings_vulnerability_in_unit_interval():
    v = tailings_vulnerability(make_site(), RiskConfig())
    assert 0.0 <= v <= 1.0


def test_abandoned_large_close_very_high_site_scores_near_one():
    site = make_site(
        volume_m3=5e7,
        status=SiteStatus.ABANDONED,
        declared_risk=RiskLevel.VERY_HIGH,
        distance_to_river_m=0.0,
    )
    assert tailings_vulnerability(site, RiskConfig()) > 0.95


def test_tailings_vulnerability_worsens_when_abandoned():
    cfg = RiskConfig()
    active = tailings_vulnerability(make_site(status=SiteStatus.ACTIVE), cfg)
    abandoned = tailings_vulnerability(make_site(status=SiteStatus.ABANDONED), cfg)
    assert abandoned > active


# --- trigger ---------------------------------------------------------------


def test_trigger_is_zero_for_calm_context():
    ctx = HazardContext(seasonal_rainfall_anomaly=RainfallAnomaly.BELOW_NORMAL)
    assert trigger_factor(ctx, RiskConfig()) == 0.0


def test_trigger_increases_with_enfen_and_rain_anomaly():
    cfg = RiskConfig()
    low = trigger_factor(HazardContext(enfen_magnitude=EnfenMagnitude.WEAK), cfg)
    high = trigger_factor(
        HazardContext(
            enfen_magnitude=EnfenMagnitude.EXTRAORDINARY,
            seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL,
        ),
        cfg,
    )
    assert 0.0 <= low < high <= 1.0


def test_trigger_reflects_short_term_warning_and_rain():
    cfg = RiskConfig()
    ctx = HazardContext(senamhi_level=SenamhiLevel.RED, observed_rain_24h_mm=60.0)
    assert trigger_factor(ctx, cfg) == pytest.approx(1.0)


# --- score -----------------------------------------------------------------


def test_score_is_product_of_factors():
    score = compute_risk_score(trigger=0.5, susceptibility=0.4, vulnerability=0.5, exposure=0.5, config=RiskConfig(epsilon=0.01))
    assert score.value == pytest.approx(0.5 * 0.4 * 0.5 * 0.5)
    assert score.factors["trigger"].value == 0.5
    assert set(score.factors) == {"trigger", "susceptibility", "tailings_vulnerability", "downstream_exposure"}


def test_zero_factor_is_floored_by_epsilon_not_silently_zeroing():
    score = compute_risk_score(trigger=0.0, susceptibility=0.8, vulnerability=0.8, exposure=0.8, config=RiskConfig(epsilon=0.05))
    assert score.value > 0.0
    assert score.value == pytest.approx(0.05 * 0.8 * 0.8 * 0.8)
    assert score.factors["trigger"].raw == 0.0
    assert score.factors["trigger"].value == 0.05
    assert score.factors["trigger"].floored is True


def test_missing_factor_uses_epsilon_and_is_flagged():
    score = compute_risk_score(trigger=0.5, susceptibility=0.5, vulnerability=0.5, exposure=None, config=RiskConfig(epsilon=0.1))
    factor = score.factors["downstream_exposure"]
    assert factor.raw is None
    assert factor.value == 0.1
    assert factor.missing is True
    assert "downstream_exposure" in score.missing_factors


def test_epsilon_zero_allows_true_zero():
    score = compute_risk_score(trigger=0.0, susceptibility=1, vulnerability=1, exposure=1, config=RiskConfig(epsilon=0.0))
    assert score.value == 0.0


def test_factors_are_clamped_to_unit_interval():
    score = compute_risk_score(trigger=2.0, susceptibility=1.5, vulnerability=1.0, exposure=1.0, config=RiskConfig())
    assert score.value == pytest.approx(1.0)


def test_score_to_dict_exposes_breakdown():
    score = compute_risk_score(trigger=0.5, susceptibility=0.5, vulnerability=0.5, exposure=0.5, config=RiskConfig(epsilon=0.01))
    data = score.to_dict()
    assert math.isclose(data["value"], 0.0625)
    assert data["factors"]["trigger"]["value"] == 0.5
    assert data["missing_factors"] == []


def test_config_validates_epsilon_and_volume_range():
    with pytest.raises(ValueError):
        RiskConfig(epsilon=-0.1)
    with pytest.raises(ValueError):
        RiskConfig(epsilon=1.0)
    with pytest.raises(ValueError):
        RiskConfig(volume_min_m3=10, volume_max_m3=10)


# --- static risk (used by the PREPARATION tier) ------------------------------


def test_static_risk_is_product_without_trigger():
    from willay.domain.risk import compute_static_risk

    value = compute_static_risk(susceptibility=0.5, vulnerability=0.5, exposure=0.8, config=RiskConfig(epsilon=0.01))
    assert value == pytest.approx(0.5 * 0.5 * 0.8)


def test_static_risk_floors_missing_exposure_with_epsilon():
    from willay.domain.risk import compute_static_risk

    value = compute_static_risk(susceptibility=0.5, vulnerability=0.5, exposure=None, config=RiskConfig(epsilon=0.1))
    assert value == pytest.approx(0.5 * 0.5 * 0.1)


# --- unknown categorical attributes use documented neutral defaults ---------


def test_unknown_declared_risk_is_neutral_between_low_and_very_high():
    unknown = declared_risk_factor(RiskLevel.UNKNOWN)
    assert unknown == NEUTRAL_DECLARED_RISK_FACTOR
    assert declared_risk_factor(RiskLevel.LOW) < unknown < declared_risk_factor(RiskLevel.VERY_HIGH)


def test_unknown_status_is_neutral_between_active_and_abandoned():
    unknown = status_factor(SiteStatus.UNKNOWN)
    assert unknown == NEUTRAL_STATUS_FACTOR
    assert status_factor(SiteStatus.ACTIVE) < unknown < status_factor(SiteStatus.ABANDONED)


def test_site_with_unknown_attributes_is_less_vulnerable_than_a_confirmed_worst_case():
    cfg = RiskConfig()
    worst = make_site(status=SiteStatus.ABANDONED, declared_risk=RiskLevel.VERY_HIGH)
    unknown = make_site(status=SiteStatus.UNKNOWN, declared_risk=RiskLevel.UNKNOWN)
    assert tailings_vulnerability(unknown, cfg) < tailings_vulnerability(worst, cfg)

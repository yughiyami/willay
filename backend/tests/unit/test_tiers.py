import pytest

from willay.domain.entities import (
    AlertTier,
    EnfenMagnitude,
    HazardContext,
    RainfallAnomaly,
    SenamhiLevel,
)
from willay.domain.tiers import TierConfig, evaluate_tier

CFG = TierConfig(
    preparation_site_risk_threshold=0.5,
    attention_rain_72h_mm=60.0,
    evacuation_rain_24h_mm=20.0,
    evacuation_water_rise_cm=30.0,
)


def prep_ctx(**overrides) -> HazardContext:
    base = dict(
        enfen_magnitude=EnfenMagnitude.STRONG,
        seasonal_rainfall_anomaly=RainfallAnomaly.ABOVE_NORMAL,
    )
    base.update(overrides)
    return HazardContext(**base)


# --- PREPARATION -----------------------------------------------------------


def test_default_context_is_none():
    result = evaluate_tier(HazardContext(), site_static_risk=0.9, config=CFG)
    assert result.tier is AlertTier.NONE
    assert result.triggered == ()


def test_preparation_when_all_three_conditions_hold():
    result = evaluate_tier(prep_ctx(), site_static_risk=0.5, config=CFG)
    assert result.tier is AlertTier.PREPARATION


def test_preparation_accepts_extraordinary_magnitude():
    ctx = prep_ctx(enfen_magnitude=EnfenMagnitude.EXTRAORDINARY)
    assert evaluate_tier(ctx, 0.8, CFG).tier is AlertTier.PREPARATION


@pytest.mark.parametrize("magnitude", [EnfenMagnitude.NEUTRAL, EnfenMagnitude.WEAK, EnfenMagnitude.MODERATE])
def test_no_preparation_below_strong(magnitude):
    ctx = prep_ctx(enfen_magnitude=magnitude)
    assert evaluate_tier(ctx, 0.9, CFG).tier is AlertTier.NONE


@pytest.mark.parametrize("anomaly", [RainfallAnomaly.NORMAL, RainfallAnomaly.BELOW_NORMAL])
def test_no_preparation_without_above_normal_rain(anomaly):
    ctx = prep_ctx(seasonal_rainfall_anomaly=anomaly)
    assert evaluate_tier(ctx, 0.9, CFG).tier is AlertTier.NONE


def test_no_preparation_when_site_risk_below_threshold():
    assert evaluate_tier(prep_ctx(), 0.49, CFG).tier is AlertTier.NONE


# --- ATTENTION -------------------------------------------------------------


@pytest.mark.parametrize("level", [SenamhiLevel.ORANGE, SenamhiLevel.RED])
def test_attention_on_orange_or_red_warning(level):
    ctx = HazardContext(senamhi_level=level)
    assert evaluate_tier(ctx, 0.0, CFG).tier is AlertTier.ATTENTION


@pytest.mark.parametrize("level", [SenamhiLevel.GREEN, SenamhiLevel.YELLOW])
def test_no_attention_on_green_or_yellow_without_rain(level):
    ctx = HazardContext(senamhi_level=level, forecast_rain_72h_mm=59.9)
    assert evaluate_tier(ctx, 0.0, CFG).tier is AlertTier.NONE


def test_attention_on_forecast_rain_threshold():
    ctx = HazardContext(forecast_rain_72h_mm=60.0)
    assert evaluate_tier(ctx, 0.0, CFG).tier is AlertTier.ATTENTION


def test_attention_outranks_preparation_but_both_are_reported():
    ctx = prep_ctx(senamhi_level=SenamhiLevel.ORANGE)
    result = evaluate_tier(ctx, 0.9, CFG)
    assert result.tier is AlertTier.ATTENTION
    assert AlertTier.PREPARATION in result.triggered
    assert AlertTier.ATTENTION in result.triggered


# --- EVACUATION ------------------------------------------------------------


def evac_ctx(**overrides) -> HazardContext:
    base = dict(observed_rain_24h_mm=25.0, water_rise_cm=40.0)
    base.update(overrides)
    return HazardContext(**base)


def test_evacuation_conditions_without_confirmation_are_pending():
    result = evaluate_tier(evac_ctx(operator_confirmed=False), 0.9, CFG)
    assert result.tier is AlertTier.PENDING_CONFIRMATION
    assert result.tier is not AlertTier.EVACUATION


def test_evacuation_requires_operator_confirmation_flag():
    result = evaluate_tier(evac_ctx(operator_confirmed=True), 0.9, CFG)
    assert result.tier is AlertTier.EVACUATION


def test_evacuation_via_watcher_report_instead_of_water_rise():
    ctx = evac_ctx(water_rise_cm=None, watcher_report=True, operator_confirmed=True)
    assert evaluate_tier(ctx, 0.9, CFG).tier is AlertTier.EVACUATION


def test_evacuation_boundary_values_are_inclusive():
    ctx = HazardContext(observed_rain_24h_mm=20.0, water_rise_cm=30.0, operator_confirmed=True)
    assert evaluate_tier(ctx, 0.0, CFG).tier is AlertTier.EVACUATION


def test_no_evacuation_when_rain_below_20mm_even_if_confirmed():
    ctx = evac_ctx(observed_rain_24h_mm=19.9, operator_confirmed=True)
    assert evaluate_tier(ctx, 0.9, CFG).tier is AlertTier.NONE


def test_no_evacuation_without_water_rise_or_watcher_even_if_confirmed():
    ctx = evac_ctx(water_rise_cm=10.0, watcher_report=False, operator_confirmed=True)
    assert evaluate_tier(ctx, 0.9, CFG).tier is AlertTier.NONE


def test_missing_water_rise_measurement_is_not_treated_as_zero_evidence_of_safety():
    ctx = evac_ctx(water_rise_cm=None, watcher_report=False, operator_confirmed=True)
    assert evaluate_tier(ctx, 0.9, CFG).tier is AlertTier.NONE


def test_evacuation_outranks_attention_and_preparation():
    ctx = prep_ctx(
        senamhi_level=SenamhiLevel.RED,
        observed_rain_24h_mm=30.0,
        water_rise_cm=50.0,
        operator_confirmed=True,
    )
    result = evaluate_tier(ctx, 0.9, CFG)
    assert result.tier is AlertTier.EVACUATION
    assert set(result.triggered) >= {AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION}


def test_pending_confirmation_keeps_lower_tiers_in_triggered():
    ctx = evac_ctx(senamhi_level=SenamhiLevel.ORANGE)
    result = evaluate_tier(ctx, 0.0, CFG)
    assert result.tier is AlertTier.PENDING_CONFIRMATION
    assert AlertTier.ATTENTION in result.triggered
    assert AlertTier.EVACUATION not in result.triggered


def test_reasons_are_human_readable_and_non_empty_when_triggered():
    result = evaluate_tier(evac_ctx(), 0.9, CFG)
    assert result.reasons
    assert all(isinstance(r, str) and r for r in result.reasons)


def test_config_rejects_invalid_values():
    with pytest.raises(ValueError):
        TierConfig(preparation_site_risk_threshold=1.5)
    with pytest.raises(ValueError):
        TierConfig(evacuation_rain_24h_mm=-1)

from willay.application.evaluate_alert_tier import evaluate_alert_tier
from willay.domain.entities import AlertTier, HazardContext, SenamhiLevel
from willay.domain.tiers import TierConfig


def test_use_case_delegates_to_deterministic_rules():
    result = evaluate_alert_tier(HazardContext(senamhi_level=SenamhiLevel.RED), site_static_risk=0.1)
    assert result.tier is AlertTier.ATTENTION


def test_use_case_never_returns_evacuation_without_operator_confirmation():
    ctx = HazardContext(observed_rain_24h_mm=80, water_rise_cm=100, watcher_report=True, operator_confirmed=False)
    assert evaluate_alert_tier(ctx, site_static_risk=1.0).tier is AlertTier.PENDING_CONFIRMATION


def test_use_case_honors_custom_config():
    ctx = HazardContext(forecast_rain_72h_mm=40)
    assert evaluate_alert_tier(ctx, 0.0, TierConfig(attention_rain_72h_mm=30)).tier is AlertTier.ATTENTION
    assert evaluate_alert_tier(ctx, 0.0, TierConfig(attention_rain_72h_mm=50)).tier is AlertTier.NONE

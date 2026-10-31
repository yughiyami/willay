"""Request schemas for the HTTP API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from willay.domain.entities import EnfenMagnitude, HazardContext, RainfallAnomaly, SenamhiLevel
from willay.domain.tiers import TierConfig


class ThresholdsIn(BaseModel):
    preparation_site_risk_threshold: float | None = Field(default=None, ge=0, le=1)
    attention_rain_72h_mm: float | None = Field(default=None, ge=0)
    evacuation_rain_24h_mm: float | None = Field(default=None, ge=0)
    evacuation_water_rise_cm: float | None = Field(default=None, ge=0)

    def to_config(self) -> TierConfig:
        overrides = {k: v for k, v in self.model_dump().items() if v is not None}
        return TierConfig(**overrides)


class EvaluateAlertIn(BaseModel):
    """Inputs for a tier evaluation.

    Provide `site_static_risk` explicitly and/or a `site_id` (which also enables the message
    composition). An explicit `site_static_risk` wins over the one computed from the site.
    """

    site_id: str | None = None
    site_static_risk: float | None = Field(default=None, ge=0, le=1)
    enfen_magnitude: EnfenMagnitude = EnfenMagnitude.NEUTRAL
    seasonal_rainfall_anomaly: RainfallAnomaly = RainfallAnomaly.NORMAL
    senamhi_level: SenamhiLevel = SenamhiLevel.GREEN
    forecast_rain_72h_mm: float = Field(default=0.0, ge=0)
    observed_rain_24h_mm: float = Field(default=0.0, ge=0)
    water_rise_cm: float | None = Field(default=None, ge=0)
    watcher_report: bool = False
    operator_confirmed: bool = False
    thresholds: ThresholdsIn | None = None
    language: str | None = Field(
        default=None,
        pattern=r"^[a-z]{2,3}$",
        description="Message language: es (default for households), en, or a code with a human-reviewed template (e.g. qu)",
    )

    @model_validator(mode="after")
    def _needs_site_or_static_risk(self) -> "EvaluateAlertIn":
        if self.site_id is None and self.site_static_risk is None:
            raise ValueError("provide site_id or site_static_risk")
        return self

    def to_context(self) -> HazardContext:
        return HazardContext(
            enfen_magnitude=self.enfen_magnitude,
            seasonal_rainfall_anomaly=self.seasonal_rainfall_anomaly,
            senamhi_level=self.senamhi_level,
            forecast_rain_72h_mm=self.forecast_rain_72h_mm,
            observed_rain_24h_mm=self.observed_rain_24h_mm,
            water_rise_cm=self.water_rise_cm,
            watcher_report=self.watcher_report,
            operator_confirmed=self.operator_confirmed,
        )


class BacktestIn(BaseModel):
    top_k_pcts: list[Annotated[int, Field(ge=1, le=100)]] = Field(default_factory=lambda: [10, 20], min_length=1)

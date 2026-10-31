from __future__ import annotations

from typing import Protocol


class WeatherPort(Protocol):
    """Rainfall forecast and observations for a location."""

    def forecast_rain_72h_mm(self, lon: float, lat: float) -> float: ...

    def observed_rain_24h_mm(self, lon: float, lat: float) -> float: ...

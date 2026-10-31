from __future__ import annotations

from typing import Protocol

from willay.domain.entities import EnfenMagnitude, RainfallAnomaly, SenamhiLevel


class HazardDataPort(Protocol):
    """Official hazard products (ENFEN, seasonal outlooks, SENAMHI warnings)."""

    def enfen_magnitude(self) -> EnfenMagnitude: ...

    def seasonal_rainfall_anomaly(self, region: str) -> RainfallAnomaly: ...

    def senamhi_level(self, province: str) -> SenamhiLevel: ...

    def official_source(self) -> str:
        """Citation of the official bulletin the hazard data came from."""
        ...

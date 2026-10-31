"""In-memory fakes for every port. Used for tests, demos and the fixture-backed API."""

from __future__ import annotations

from typing import Sequence

from willay.domain.entities import (
    EnfenMagnitude,
    RainfallAnomaly,
    SenamhiLevel,
    Settlement,
    TailingsSite,
)


class InMemorySiteRepository:
    def __init__(self, sites: Sequence[TailingsSite], settlements: Sequence[Settlement]) -> None:
        self._sites = tuple(sites)
        self._settlements = tuple(settlements)

    def list_sites(self) -> Sequence[TailingsSite]:
        return self._sites

    def get_site(self, site_id: str) -> TailingsSite | None:
        return next((s for s in self._sites if s.id == site_id), None)

    def list_settlements(self) -> Sequence[Settlement]:
        return self._settlements


class InMemoryHazardData:
    def __init__(
        self,
        enfen: EnfenMagnitude = EnfenMagnitude.NEUTRAL,
        anomaly: RainfallAnomaly = RainfallAnomaly.NORMAL,
        senamhi: SenamhiLevel = SenamhiLevel.GREEN,
        source: str = "in-memory fake (not an official bulletin)",
    ) -> None:
        self._enfen, self._anomaly, self._senamhi, self._source = enfen, anomaly, senamhi, source

    def enfen_magnitude(self) -> EnfenMagnitude:
        return self._enfen

    def seasonal_rainfall_anomaly(self, region: str) -> RainfallAnomaly:
        return self._anomaly

    def senamhi_level(self, province: str) -> SenamhiLevel:
        return self._senamhi

    def official_source(self) -> str:
        return self._source


class InMemoryWeather:
    def __init__(self, forecast_72h_mm: float = 0.0, observed_24h_mm: float = 0.0) -> None:
        self._forecast, self._observed = forecast_72h_mm, observed_24h_mm

    def forecast_rain_72h_mm(self, lon: float, lat: float) -> float:
        return self._forecast

    def observed_rain_24h_mm(self, lon: float, lat: float) -> float:
        return self._observed


class RecordingMessaging:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


class EchoLLM:
    """Returns its input unchanged. Keeps the pipeline deterministic offline."""

    def rephrase(self, text: str, language: str = "en") -> str:
        return text

    def explain(self, facts: str, language: str = "en") -> str:
        return facts


class FakeSegmentation:
    def __init__(self, rise_cm: float | None = 0.0) -> None:
        self._rise_cm = rise_cm

    def measure_water_rise_cm(self, reference_frame: bytes, current_frame: bytes) -> float | None:
        return self._rise_cm


class FakeImage:
    def generate(self, prompt: str) -> bytes:
        return b"FAKE-IMAGE:" + prompt.encode("utf-8")

from willay.adapters.fixture_loader import load_rimac_fixture
from willay.adapters.in_memory import (
    EchoLLM,
    FakeImage,
    FakeSegmentation,
    InMemoryHazardData,
    InMemorySiteRepository,
    InMemoryWeather,
    RecordingMessaging,
)
from willay.domain.entities import EnfenMagnitude, RainfallAnomaly, SenamhiLevel


def test_site_repository_serves_fixture_data():
    fixture = load_rimac_fixture()
    repo = InMemorySiteRepository(fixture.sites, fixture.settlements)
    assert len(repo.list_sites()) == len(fixture.sites)
    assert repo.get_site("syn-001").id == "syn-001"
    assert repo.get_site("nope") is None
    assert repo.list_settlements()


def test_hazard_data_returns_configured_values_and_source():
    data = InMemoryHazardData(
        enfen=EnfenMagnitude.MODERATE,
        anomaly=RainfallAnomaly.ABOVE_NORMAL,
        senamhi=SenamhiLevel.ORANGE,
        source="SYNTHETIC",
    )
    assert data.enfen_magnitude() is EnfenMagnitude.MODERATE
    assert data.seasonal_rainfall_anomaly("any") is RainfallAnomaly.ABOVE_NORMAL
    assert data.senamhi_level("any") is SenamhiLevel.ORANGE
    assert data.official_source() == "SYNTHETIC"


def test_weather_returns_configured_values():
    weather = InMemoryWeather(forecast_72h_mm=12.0, observed_24h_mm=3.0)
    assert weather.forecast_rain_72h_mm(-76.0, -11.0) == 12.0
    assert weather.observed_rain_24h_mm(-76.0, -11.0) == 3.0


def test_messaging_records_sent_messages():
    messaging = RecordingMessaging()
    messaging.send("+51000000000", "hello")
    assert messaging.sent == [("+51000000000", "hello")]


def test_echo_llm_returns_text_unchanged():
    llm = EchoLLM()
    assert llm.rephrase("abc") == "abc"
    assert llm.explain("facts") == "facts"


def test_fake_segmentation_and_image():
    assert FakeSegmentation(rise_cm=42.0).measure_water_rise_cm(b"a", b"b") == 42.0
    assert FakeSegmentation(rise_cm=None).measure_water_rise_cm(b"a", b"b") is None
    assert isinstance(FakeImage().generate("prompt"), bytes)

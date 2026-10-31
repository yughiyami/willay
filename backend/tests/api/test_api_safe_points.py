"""Safe points wired into the alert message: proposed points are labelled, absent ones never say "no definido"."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from willay.adapters.in_memory import InMemorySiteRepository
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies
from willay.domain.safe_point import SafePoint

PROPOSED = SafePoint(-76.0, -12.0, 140.0, 1230.0, 47.0, 42.0, 410.0, 8.0)
ATTENTION = {"site_id": "syn-001", "senamhi_level": "orange"}
LABEL = "propuesto — validar con la municipalidad"


def client_with(**changes) -> TestClient:
    deps = synthetic_dependencies()
    settlements = [dataclasses.replace(s, **changes) for s in deps.repository.list_settlements()]
    deps.repository = InMemorySiteRepository(deps.repository.list_sites(), settlements)
    return TestClient(create_app(deps))


def test_a_proposed_safe_point_reaches_the_household_message_with_its_label():
    body = client_with(safe_point=None, proposed_safe_point=PROPOSED).post("/alerts/evaluate", json=ATTENTION).json()
    text = body["message"]["text"]
    assert LABEL in text and "1.2 km al noreste" in text
    assert body["message"]["safe_point"]["kind"] == "proposed"
    assert body["message"]["safe_point"]["lon"] == pytest.approx(-76.0)
    assert body["message"]["safe_point"]["label"] == LABEL


def test_without_any_safe_point_the_message_asks_the_municipality():
    body = client_with(safe_point=None).post("/alerts/evaluate", json=ATTENTION).json()
    assert "Consulte a su municipalidad el punto seguro" in body["message"]["text"]
    assert "no definido" not in body["message"]["text"].lower()
    assert body["message"]["safe_point"]["kind"] == "none"


def test_a_curated_safe_point_is_reported_as_curated():
    body = client_with(proposed_safe_point=PROPOSED).post("/alerts/evaluate", json=ATTENTION).json()
    assert body["message"]["safe_point"]["kind"] == "curated"
    assert "propuesto" not in body["message"]["text"]

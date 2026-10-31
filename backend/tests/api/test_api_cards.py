"""POST /cards/evacuation with an injected image generator (no network)."""

import base64

import pytest
from fastapi.testclient import TestClient

from willay.adapters.meta_model_api import MetaApiError
from willay.adapters.meta_usage import BudgetExceededError
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies

PAYLOAD = {
    "settlement_name": "Caserío Sintético Uno",
    "distance_km": 3.4,
    "safe_point": "Loma del mirador",
    "tier": "EVACUATION",
    "synthetic_household": True,
}


class FakeImage:
    def __init__(self, error=None):
        self.error, self.prompts = error, []

    def generate(self, prompt, size=None):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return b"\x89PNG-card"


def client_with(image, tmp_path=None) -> TestClient:
    deps = synthetic_dependencies()
    deps.image_generator = image
    deps.derived_dir = tmp_path
    return TestClient(create_app(deps))


def test_returns_the_card_with_intended_text_and_a_review_flag(tmp_path):
    image = FakeImage()
    response = client_with(image, tmp_path).post("/cards/evacuation", json=PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert base64.b64decode(body["image_png_base64"]) == b"\x89PNG-card"
    assert body["intended_text"]["title"] == "EVACÚE AHORA"
    assert body["review_required"] is True
    assert body["tier"] == "EVACUATION"
    assert len(image.prompts) == 1


def test_the_card_is_saved_under_derived_cards(tmp_path):
    body = client_with(FakeImage(), tmp_path).post("/cards/evacuation", json=PAYLOAD).json()
    saved = tmp_path / "cards" / body["saved_as"]
    assert saved.read_bytes() == b"\x89PNG-card"


def test_requires_the_synthetic_household_confirmation():
    for payload in ({**PAYLOAD, "synthetic_household": False}, {k: v for k, v in PAYLOAD.items() if k != "synthetic_household"}):
        assert client_with(FakeImage()).post("/cards/evacuation", json=payload).status_code in (400, 422)


@pytest.mark.parametrize("tier", ["NONE", "PENDING_CONFIRMATION", "BANANA"])
def test_only_household_tiers_are_accepted(tier):
    assert client_with(FakeImage()).post("/cards/evacuation", json={**PAYLOAD, "tier": tier}).status_code == 422


def test_negative_distance_is_rejected():
    assert client_with(FakeImage()).post("/cards/evacuation", json={**PAYLOAD, "distance_km": -2}).status_code == 422


def test_unavailable_without_an_image_generator():
    assert client_with(None).post("/cards/evacuation", json=PAYLOAD).status_code == 503


@pytest.mark.parametrize("error,status", [(MetaApiError("boom", 500), 502), (BudgetExceededError("over budget"), 429)])
def test_upstream_failures_map_to_clean_errors(error, status):
    assert client_with(FakeImage(error)).post("/cards/evacuation", json=PAYLOAD).status_code == status


# --- safe point resolved from a settlement ------------------------------------------

import dataclasses

from willay.adapters.in_memory import InMemorySiteRepository
from willay.domain.safe_point import SafePoint

PROPOSED = SafePoint(-76.0, -12.0, 140.0, 1230.0, 47.0, 42.0, 410.0, 8.0)
WITHOUT_SAFE_POINT = {k: v for k, v in PAYLOAD.items() if k != "safe_point"}


def client_with_settlements(image, **changes) -> TestClient:
    deps = synthetic_dependencies()
    settlements = [dataclasses.replace(s, **changes) for s in deps.repository.list_settlements()]
    deps.repository = InMemorySiteRepository(deps.repository.list_sites(), settlements)
    deps.image_generator = image
    return TestClient(create_app(deps))


def test_a_settlement_id_supplies_the_proposed_safe_point_with_its_label():
    client = client_with_settlements(FakeImage(), safe_point=None, proposed_safe_point=PROPOSED)
    body = client.post("/cards/evacuation", json={**WITHOUT_SAFE_POINT, "settlement_id": "set-001"}).json()
    assert body["intended_text"]["safe_point"] == "Punto seguro: a 1.2 km al noreste (propuesto — validar con la municipalidad)"


def test_a_settlement_without_any_safe_point_gets_the_municipality_line():
    client = client_with_settlements(FakeImage(), safe_point=None)
    body = client.post("/cards/evacuation", json={**WITHOUT_SAFE_POINT, "settlement_id": "set-001"}).json()
    assert body["intended_text"]["safe_point"] == "Consulte a su municipalidad el punto seguro"


def test_an_unknown_settlement_id_is_rejected():
    response = client_with(FakeImage()).post("/cards/evacuation", json={**WITHOUT_SAFE_POINT, "settlement_id": "nope"})
    assert response.status_code == 404

"""Evacuation card for a SYNTHETIC household: deterministic Spanish text, one generated image."""

import pytest

from willay.application.evacuation_card import CardRequest, build_card, card_text_lines
from willay.domain.entities import AlertTier
from willay.domain.safe_point import SafePoint


def request(tier=AlertTier.EVACUATION, **kw) -> CardRequest:
    base = dict(settlement_name="Caserío Sintético Uno", distance_km=3.4, safe_point="Loma del mirador", tier=tier)
    base.update(kw)
    return CardRequest(**base)


class FakeImage:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt: str, size: str | None = None) -> bytes:
        self.prompts.append((prompt, size))
        return b"\x89PNG-card"


def test_text_lines_are_simple_spanish_with_the_given_facts():
    lines = card_text_lines(request())
    assert lines["title"] == "EVACÚE AHORA"
    assert lines["community"] == "Comunidad: Caserío Sintético Uno"
    assert lines["distance"] == "Distancia al relave: 3,4 km"
    assert lines["safe_point"] == "Punto seguro: Loma del mirador"
    assert "SENAMHI" in lines["footer"] and "INDECI" in lines["footer"] and "ENFEN" in lines["footer"]


def test_titles_follow_the_tier():
    assert card_text_lines(request(AlertTier.PREPARATION))["title"] == "PREPÁRESE"
    assert card_text_lines(request(AlertTier.ATTENTION))["title"].startswith("ATENCIÓN")


def test_missing_safe_point_asks_the_municipality_and_is_not_invented():
    lines = card_text_lines(request(safe_point=None))
    assert lines["safe_point"] == "Consulte a su municipalidad el punto seguro"
    assert "no definido" not in lines["safe_point"].lower()


def test_a_proposed_safe_point_is_described_with_its_validation_label():
    proposed = SafePoint(-76.0, -12.0, 140.0, 1230.0, 47.0, 42.0, 410.0, 8.0)
    lines = card_text_lines(request(safe_point=None, proposed_safe_point=proposed))
    assert lines["safe_point"] == "Punto seguro: a 1.2 km al noreste (propuesto — validar con la municipalidad)"


def test_a_curated_safe_point_wins_over_a_proposal():
    proposed = SafePoint(-76.0, -12.0, 140.0, 1230.0, 47.0, 42.0, 410.0, 8.0)
    assert card_text_lines(request(proposed_safe_point=proposed))["safe_point"] == "Punto seguro: Loma del mirador"


@pytest.mark.parametrize("tier", [AlertTier.NONE, AlertTier.PENDING_CONFIRMATION])
def test_cards_are_only_for_household_tiers(tier):
    with pytest.raises(ValueError):
        build_card(request(tier), FakeImage())


def test_build_card_sends_one_prompt_with_the_exact_text_and_returns_the_image():
    image = FakeImage()
    card = build_card(request(), image)
    assert card.image == b"\x89PNG-card"
    assert len(image.prompts) == 1  # one image per card
    prompt, size = image.prompts[0]
    for text in card.intended_text.values():
        assert text in prompt
    assert "Spanish" in prompt and "pictogram" in prompt.lower()
    assert "no photographs of people" in prompt.lower()
    assert size == "1024x1536"
    assert card.review_required is True


def test_negative_distance_is_rejected():
    with pytest.raises(ValueError):
        request(distance_km=-1)

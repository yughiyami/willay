"""Use case: one evacuation information card (image) for a SYNTHETIC household.

The text is deterministic Spanish (short, plain, low-literacy friendly) and the facts are given, never invented.
The image model only draws the card; the tier is an input decided by the domain rules. Because image models can
misrender text, the intended text is returned next to the image and the card is flagged `review_required`.
No personal data is ever part of a card request: only a settlement name, a distance and a safe point.
"""

from __future__ import annotations

from dataclasses import dataclass

from willay.domain.entities import AlertTier
from willay.ports.image import ImagePort

CARD_SIZE = "1024x1536"
_TITLES = {
    AlertTier.PREPARATION: "PREPÁRESE",
    AlertTier.ATTENTION: "ATENCIÓN: LLUVIAS FUERTES",
    AlertTier.EVACUATION: "EVACÚE AHORA",
}
_FOOTER = "Esta alerta complementa a SENAMHI, INDECI y ENFEN"
_NO_SAFE_POINT = "Punto seguro: consulte a Defensa Civil"


@dataclass(frozen=True)
class CardRequest:
    settlement_name: str
    distance_km: float
    safe_point: str | None
    tier: AlertTier

    def __post_init__(self) -> None:
        if self.distance_km < 0:
            raise ValueError("distance_km must be >= 0")
        if not self.settlement_name.strip():
            raise ValueError("settlement_name is required")


@dataclass(frozen=True)
class EvacuationCard:
    image: bytes
    intended_text: dict[str, str]
    prompt: str
    review_required: bool = True


def card_text_lines(request: CardRequest) -> dict[str, str]:
    if request.tier not in _TITLES:
        raise ValueError("cards exist only for the household tiers PREPARATION, ATTENTION and EVACUATION")
    km = f"{request.distance_km:.1f}".replace(".", ",")
    return {
        "title": _TITLES[request.tier],
        "community": f"Comunidad: {request.settlement_name.strip()}",
        "distance": f"Distancia al relave: {km} km",
        "safe_point": f"Punto seguro: {request.safe_point.strip()}" if request.safe_point else _NO_SAFE_POINT,
        "footer": _FOOTER,
    }


def build_prompt(lines: dict[str, str]) -> str:
    return (
        "Design ONE vertical information card (poster) for a rural Andean community in Peru, simple enough for people "
        "with low literacy: very large pictograms, high contrast, flat colors, at most five short text blocks. "
        "Show pictograms only: a river with a warning triangle, a tailings dam icon, and arrows leading uphill to a safe "
        "point flag. No photographs of people, no real logos, no maps. "
        "Render ALL text in Spanish, spelled exactly as given and in this order, with the title largest:\n"
        f"1. {lines['title']}\n2. {lines['community']}\n3. {lines['distance']}\n4. {lines['safe_point']}\n"
        f"5. (small footer) {lines['footer']}\n"
        "Do not add any other words or numbers."
    )


def build_card(request: CardRequest, image: ImagePort) -> EvacuationCard:
    lines = card_text_lines(request)
    prompt = build_prompt(lines)
    return EvacuationCard(image=image.generate(prompt, CARD_SIZE), intended_text=lines, prompt=prompt)  # type: ignore[call-arg]

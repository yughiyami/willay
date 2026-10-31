"""Use case: compose an alert message from a deterministic template.

The tier is decided elsewhere and is never changed here. An LLM may rephrase the text
only if every required fact survives; otherwise the template text is used unchanged.

Languages: Spanish (default for household messages) and English are built in. Any other language
(for example Quechua) needs a human-reviewed template supplied through `templates`; text in those
languages is never machine-generated and never passed through the LLM.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

from willay.domain.entities import AlertTier
from willay.ports.llm import LLMPort
from willay.ports.templates import ReviewedTemplatesPort, TemplateNotAvailable

HOUSEHOLD_LANGUAGE = "es"
OPERATOR_LANGUAGE = "en"
BUILTIN_LANGUAGES = ("es", "en")

DISCLAIMER = {
    "en": "This alert complements the official SENAMHI, INDECI and ENFEN alerts and does not replace them.",
    "es": "Esta alerta complementa las alertas oficiales de SENAMHI, INDECI y ENFEN y no las reemplaza.",
}
_OFFICIAL_BODIES = ("SENAMHI", "INDECI", "ENFEN")

_ACTION = {
    "en": {
        AlertTier.PREPARATION: "Conditions favor heavy rains in the coming weeks. Review your evacuation route and emergency kit.",
        AlertTier.ATTENTION: "Heavy rain is possible within 1-3 days. Follow official bulletins and be ready to move.",
        AlertTier.EVACUATION: "EVACUATE NOW to the safe point. Do not cross the river or its banks.",
        AlertTier.PENDING_CONFIRMATION: (
            "Evacuation conditions detected. Operator action required: confirm or dismiss the evacuation alert."
        ),
    },
    "es": {
        AlertTier.PREPARATION: (
            "Las condiciones favorecen lluvias intensas en las próximas semanas. "
            "Revise su ruta de evacuación y su mochila de emergencia."
        ),
        AlertTier.ATTENTION: (
            "Es posible que ocurran lluvias intensas en las próximas 24 a 72 horas. "
            "Siga los boletines oficiales y manténgase listo para desplazarse."
        ),
        AlertTier.EVACUATION: "EVACÚE AHORA hacia el punto seguro. No cruce el río ni sus riberas.",
        AlertTier.PENDING_CONFIRMATION: (
            "Se detectaron condiciones de evacuación. Se requiere la acción del operador: "
            "confirme o descarte la alerta de evacuación."
        ),
    },
}

_LABELS = {
    "en": {
        "distance": "Distance from the site",
        "safe_point": "Safe point",
        "source": "Official source",
        "no_safe_point": "not defined for this location (contact local civil defense)",
    },
    "es": {
        "distance": "Distancia desde el relave",
        "safe_point": "Punto seguro",
        "source": "Fuente oficial",
        "no_safe_point": "no definido para esta ubicación (comuníquese con Defensa Civil local)",
    },
}

_PLACEHOLDERS = {"tier", "site_name", "distance_km", "safe_point", "official_source", "disclaimer"}


@dataclass(frozen=True)
class AlertMessageInput:
    tier: AlertTier
    site_name: str
    distance_km: float
    safe_point: str | None
    official_source: str


@dataclass(frozen=True)
class AlertMessage:
    tier: AlertTier
    text: str
    audience: str  # "public" or "operator"
    rephrased: bool
    language: str = "en"


def action_text(tier: AlertTier, language: str) -> str:
    """The pre-approved action sentence of a tier in a built-in language."""
    return _ACTION[language][tier]


def _template(data: AlertMessageInput, language: str) -> str:
    labels = _LABELS[language]
    safe_point = data.safe_point or labels["no_safe_point"]
    return (
        f"[{data.tier.value}] {data.site_name}: {_ACTION[language][data.tier]} "
        f"{labels['distance']}: {data.distance_km:.1f} km. "
        f"{labels['safe_point']}: {safe_point}. "
        f"{labels['source']}: {data.official_source}. "
        f"{DISCLAIMER[language]}"
    )


def _required_facts(data: AlertMessageInput) -> list[str]:
    facts = [data.site_name, f"{data.distance_km:.1f}", data.official_source, *_OFFICIAL_BODIES]
    if data.safe_point:
        facts.append(data.safe_point)
    return facts


def _from_reviewed_template(
    data: AlertMessageInput, language: str, templates: ReviewedTemplatesPort | None
) -> str:
    reviewed = templates.get(language) if templates is not None else None
    if reviewed is None:
        raise TemplateNotAvailable(
            language,
            "a human-reviewed template file is required for this language; "
            "machine-generated text is not allowed",
        )
    text = reviewed.messages.get(data.tier.value)
    if text is None:
        raise TemplateNotAvailable(language, f"the reviewed templates have no message for tier {data.tier.value}")
    fields = {name for _, name, _, _ in string.Formatter().parse(text) if name}
    unknown = fields - _PLACEHOLDERS
    if unknown:
        raise TemplateNotAvailable(language, f"the reviewed template uses unknown placeholders: {sorted(unknown)}")
    return text.format(
        tier=data.tier.value,
        site_name=data.site_name,
        distance_km=f"{data.distance_km:.1f}",
        safe_point=data.safe_point or _LABELS["es"]["no_safe_point"],
        official_source=data.official_source,
        disclaimer=DISCLAIMER["es"],
    )


def compose_alert_message(
    data: AlertMessageInput,
    llm: LLMPort | None = None,
    language: str | None = None,
    templates: ReviewedTemplatesPort | None = None,
) -> AlertMessage:
    """Compose the message. `language` defaults to Spanish for households and English for operators.

    Raises TemplateNotAvailable for a language without built-in text and without a reviewed template.
    """
    if data.tier is AlertTier.NONE:
        raise ValueError("no message is composed for tier NONE")
    audience = "operator" if data.tier is AlertTier.PENDING_CONFIRMATION else "public"
    language = language or (OPERATOR_LANGUAGE if audience == "operator" else HOUSEHOLD_LANGUAGE)

    if language not in BUILTIN_LANGUAGES:
        text = _from_reviewed_template(data, language, templates)
        return AlertMessage(tier=data.tier, text=text, audience=audience, rephrased=False, language=language)

    text = _template(data, language)
    if llm is not None:
        try:
            candidate = llm.rephrase(text, language)
        except Exception:  # any LLM failure must fall back to the deterministic template
            candidate = None
        if candidate and all(fact in candidate for fact in _required_facts(data)):
            return AlertMessage(tier=data.tier, text=candidate, audience=audience, rephrased=True, language=language)
    return AlertMessage(tier=data.tier, text=text, audience=audience, rephrased=False, language=language)

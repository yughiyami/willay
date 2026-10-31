"""Use case: compose an alert message from a deterministic template.

The tier is decided elsewhere and is never changed here. An LLM may rephrase the text
only if every required fact survives; otherwise the template text is used unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from willay.domain.entities import AlertTier
from willay.ports.llm import LLMPort

DISCLAIMER = (
    "This alert complements the official SENAMHI, INDECI and ENFEN alerts and does not replace them."
)
_OFFICIAL_BODIES = ("SENAMHI", "INDECI", "ENFEN")

_ACTION = {
    AlertTier.PREPARATION: "Conditions favor heavy rains in the coming weeks. Review your evacuation route and emergency kit.",
    AlertTier.ATTENTION: "Heavy rain is possible within 1-3 days. Follow official bulletins and be ready to move.",
    AlertTier.EVACUATION: "EVACUATE NOW to the safe point. Do not cross the river or its banks.",
    AlertTier.PENDING_CONFIRMATION: (
        "Evacuation conditions detected. Operator action required: confirm or dismiss the evacuation alert."
    ),
}


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


def _template(data: AlertMessageInput) -> str:
    safe_point = data.safe_point or "not defined for this location (contact local civil defense)"
    return (
        f"[{data.tier.value}] {data.site_name}: {_ACTION[data.tier]} "
        f"Distance from the site: {data.distance_km:.1f} km. "
        f"Safe point: {safe_point}. "
        f"Official source: {data.official_source}. "
        f"{DISCLAIMER}"
    )


def _required_facts(data: AlertMessageInput) -> list[str]:
    facts = [data.site_name, f"{data.distance_km:.1f}", data.official_source, *_OFFICIAL_BODIES]
    if data.safe_point:
        facts.append(data.safe_point)
    return facts


def compose_alert_message(data: AlertMessageInput, llm: LLMPort | None = None, language: str = "en") -> AlertMessage:
    if data.tier is AlertTier.NONE:
        raise ValueError("no message is composed for tier NONE")
    audience = "operator" if data.tier is AlertTier.PENDING_CONFIRMATION else "public"
    text = _template(data)
    if llm is not None:
        try:
            candidate = llm.rephrase(text, language)
        except Exception:  # any LLM failure must fall back to the deterministic template
            candidate = None
        if candidate and all(fact in candidate for fact in _required_facts(data)):
            return AlertMessage(tier=data.tier, text=candidate, audience=audience, rephrased=True)
    return AlertMessage(tier=data.tier, text=text, audience=audience, rephrased=False)

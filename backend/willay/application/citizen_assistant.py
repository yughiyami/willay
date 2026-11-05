"""Use case: the Spanish Q&A a household gets on WhatsApp (text, or a transcribed voice note).

Same guardrails as the operator agent, stricter in one way: the model has NO tools, so it can read nothing and
change nothing. An answer that states an alert-tier code or orders an evacuation is replaced, every answer cites the
official sources, and the whole reply fits 600 characters. Alert tiers come only from the domain rules, and an
evacuation is only ever confirmed by a human operator.
"""

from __future__ import annotations

import re

from willay.application.operator_agent import guard_tier_claims
from willay.ports.agent import AgentModelPort

MAX_ANSWER_CHARS = 600
FOOTER_ES = "Willay complementa a SENAMHI e INDECI (INDECI: 115)."
FALLBACK_ES = (
    "No puedo responder eso con seguridad. Siga las indicaciones oficiales de INDECI (115), SENAMHI y su "
    "municipalidad. Willay complementa esas alertas."
)
SYSTEM_PROMPT_ES = (
    "Eres el asistente de WhatsApp de Willay Relaves, un sistema de alerta temprana por El Niño para relaves "
    "mineros y comunidades río abajo en la cuenca del Rímac (Perú). Respondes a familias, en español sencillo y "
    "amable, con máximo 3 frases cortas. No tienes herramientas ni datos en vivo: no inventes cifras, horas ni "
    "estados de ríos o relaves. Los niveles de alerta (PREPARATION, ATTENTION, PENDING_CONFIRMATION, EVACUATION) "
    "los determinan reglas del sistema: nunca digas ni deduzcas un nivel. Nunca ordenes una evacuación ni digas que "
    "no hace falta evacuar: eso lo decide un operador humano y las autoridades. Da consejos generales de "
    "seguridad (revisar su punto seguro, mantener documentos y mochila listos, no cruzar ríos crecidos) y remite a "
    "las fuentes oficiales: SENAMHI, INDECI (línea 115) y su municipalidad. Willay complementa, no reemplaza, esas "
    "alertas. Si piden algo fuera de este tema, di amablemente que solo ayudas con seguridad ante huaicos y lluvias."
)

_ORDERS_EVACUATION = re.compile(
    r"\bevac[uú]e(?:n|se|nse)?\b|\b(?:debe|deben|tiene que|tienen que|hay que|debes|tienes que)\s+evacuar\b", re.IGNORECASE
)
_MENTIONS_SOURCES = re.compile(r"SENAMHI|INDECI", re.IGNORECASE)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: boundary + 1] if boundary > limit // 2 else cut.rstrip()) + ("" if boundary > limit // 2 else "…")


class CitizenAssistant:
    def __init__(self, model: AgentModelPort) -> None:
        self._model = model

    def answer(self, text: str) -> str:
        turn = self._model.run_turn(
            instructions=SYSTEM_PROMPT_ES,
            input_items=[{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            tools=[],
            purpose="whatsapp_qa",
            max_output_tokens=1500,
        )
        answer = " ".join(turn.text.split())
        if not answer:
            return FALLBACK_ES
        answer, flags = guard_tier_claims(answer, set())
        if flags or _ORDERS_EVACUATION.search(answer):
            return FALLBACK_ES
        footer = "" if (_MENTIONS_SOURCES.search(answer) and "complementa" in answer.lower()) else " " + FOOTER_ES
        return _cut(answer, MAX_ANSWER_CHARS - len(footer)) + footer

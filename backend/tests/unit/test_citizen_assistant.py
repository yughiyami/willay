"""The citizen Q&A on WhatsApp: a Spanish answer from the model, bounded by the same guardrails as the operator agent."""

from willay.application.citizen_assistant import (
    FALLBACK_ES,
    FOOTER_ES,
    MAX_ANSWER_CHARS,
    CitizenAssistant,
)
from willay.ports.agent import AgentTurn


class FakeModel:
    def __init__(self, text="Mantenga la calma y revise su punto seguro. Fuente: INDECI."):
        self.text, self.calls = text, []

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return AgentTurn(response_id="r1", text=self.text)


def test_the_model_runs_with_no_tools_and_a_spanish_safety_prompt():
    model = FakeModel()
    CitizenAssistant(model).answer("¿Qué hago si llueve fuerte?")
    call = model.calls[0]
    assert call["tools"] == [] and call["purpose"] == "whatsapp_qa"
    instructions = call["instructions"]
    for needle in ("español", "SENAMHI", "INDECI", "nunca", "evacuación"):
        assert needle in instructions
    assert call["input_items"][0]["content"][0]["text"] == "¿Qué hago si llueve fuerte?"


def test_the_answer_cites_official_sources_and_stays_within_600_characters():
    answer = CitizenAssistant(FakeModel("a" * 900)).answer("hola")
    assert len(answer) <= MAX_ANSWER_CHARS == 600
    assert FOOTER_ES in answer and "SENAMHI" in answer and "INDECI" in answer


def test_a_short_answer_gets_the_complement_footer_once():
    answer = CitizenAssistant(FakeModel("Revise su punto seguro.")).answer("hola")
    assert answer.count(FOOTER_ES) == 1


def test_an_answer_that_states_an_alert_tier_is_replaced():
    for tier in ("EVACUATION", "ATTENTION", "PREPARATION", "PENDING_CONFIRMATION"):
        assert CitizenAssistant(FakeModel(f"El nivel es {tier} para su zona.")).answer("¿qué nivel hay?") == FALLBACK_ES


def test_an_answer_that_orders_an_evacuation_is_replaced():
    for text in ("Evacúe ahora hacia el cerro.", "Debe evacuar su casa.", "Evacuen inmediatamente.", "Tiene que evacuar."):
        assert CitizenAssistant(FakeModel(text)).answer("¿qué hago?") == FALLBACK_ES


def test_an_empty_answer_falls_back():
    assert CitizenAssistant(FakeModel("   ")).answer("hola") == FALLBACK_ES


def test_the_fallback_points_to_official_channels_and_is_short():
    assert "INDECI" in FALLBACK_ES and "SENAMHI" in FALLBACK_ES and len(FALLBACK_ES) <= MAX_ANSWER_CHARS

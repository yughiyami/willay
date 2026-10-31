import pytest

from willay.application.compose_alert_message import AlertMessageInput, compose_alert_message
from willay.domain.entities import AlertTier


def make_input(tier=AlertTier.EVACUATION, **overrides) -> AlertMessageInput:
    base = dict(
        tier=tier,
        site_name="SYNTHETIC Tailings Alpha",
        distance_km=7.5,
        safe_point="Plaza Mayor",
        official_source="SENAMHI Aviso 123/2026",
    )
    base.update(overrides)
    return AlertMessageInput(**base)


class UpperCaseLLM:
    """Keeps every required field, only changes the surrounding wording."""

    def rephrase(self, text: str, language: str = "en") -> str:
        return "Friendly note: " + text

    def explain(self, facts: str, language: str = "en") -> str:
        return facts


class FieldDroppingLLM:
    def rephrase(self, text: str, language: str = "en") -> str:
        return "Please be careful out there."

    def explain(self, facts: str, language: str = "en") -> str:
        return facts


class FailingLLM:
    def rephrase(self, text: str, language: str = "en") -> str:
        raise RuntimeError("model unavailable")

    def explain(self, facts: str, language: str = "en") -> str:
        raise RuntimeError("model unavailable")


@pytest.mark.parametrize("tier", [AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION])
def test_template_contains_all_required_fields(tier):
    message = compose_alert_message(make_input(tier))
    for needle in (tier.value, "SYNTHETIC Tailings Alpha", "7.5 km", "Plaza Mayor", "SENAMHI Aviso 123/2026"):
        assert needle in message.text
    assert message.rephrased is False


def test_every_message_states_it_complements_official_alerts():
    message = compose_alert_message(make_input(AlertTier.ATTENTION))
    assert "does not replace" in message.text
    assert "SENAMHI" in message.text and "INDECI" in message.text and "ENFEN" in message.text


def test_evacuation_message_is_imperative():
    assert "EVACUATE" in compose_alert_message(make_input(AlertTier.EVACUATION)).text


def test_pending_confirmation_message_is_for_operator_only():
    message = compose_alert_message(make_input(AlertTier.PENDING_CONFIRMATION))
    assert message.audience == "operator"
    assert "confirm" in message.text.lower()


def test_public_tiers_have_public_audience():
    assert compose_alert_message(make_input(AlertTier.EVACUATION)).audience == "public"


def test_none_tier_has_no_message():
    with pytest.raises(ValueError):
        compose_alert_message(make_input(AlertTier.NONE))


def test_llm_may_rephrase_when_required_fields_survive():
    message = compose_alert_message(make_input(), llm=UpperCaseLLM())
    assert message.rephrased is True
    assert message.text.startswith("Friendly note:")


def test_llm_output_dropping_required_fields_is_rejected():
    template = compose_alert_message(make_input())
    message = compose_alert_message(make_input(), llm=FieldDroppingLLM())
    assert message.rephrased is False
    assert message.text == template.text


def test_llm_failure_falls_back_to_template():
    template = compose_alert_message(make_input())
    message = compose_alert_message(make_input(), llm=FailingLLM())
    assert message.rephrased is False
    assert message.text == template.text


def test_message_never_changes_the_tier():
    message = compose_alert_message(make_input(AlertTier.ATTENTION), llm=UpperCaseLLM())
    assert message.tier is AlertTier.ATTENTION


def test_missing_safe_point_is_stated_explicitly():
    message = compose_alert_message(make_input(safe_point=None))
    assert "not defined" in message.text.lower()

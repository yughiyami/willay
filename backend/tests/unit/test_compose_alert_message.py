import pytest

from willay.application.compose_alert_message import AlertMessageInput, compose_alert_message
from willay.ports.templates import ReviewedTemplates, TemplateNotAvailable
from willay.domain.entities import AlertTier
from willay.domain.safe_point import SafePoint


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
    message = compose_alert_message(language="en", data=make_input(tier))
    for needle in (tier.value, "SYNTHETIC Tailings Alpha", "7.5 km", "Plaza Mayor", "SENAMHI Aviso 123/2026"):
        assert needle in message.text
    assert message.rephrased is False


def test_every_message_states_it_complements_official_alerts():
    message = compose_alert_message(language="en", data=make_input(AlertTier.ATTENTION))
    assert "does not replace" in message.text
    assert "SENAMHI" in message.text and "INDECI" in message.text and "ENFEN" in message.text


def test_evacuation_message_is_imperative():
    assert "EVACUATE" in compose_alert_message(language="en", data=make_input(AlertTier.EVACUATION)).text


def test_pending_confirmation_message_is_for_operator_only():
    message = compose_alert_message(language="en", data=make_input(AlertTier.PENDING_CONFIRMATION))
    assert message.audience == "operator"
    assert "confirm" in message.text.lower()


def test_public_tiers_have_public_audience():
    assert compose_alert_message(language="en", data=make_input(AlertTier.EVACUATION)).audience == "public"


def test_none_tier_has_no_message():
    with pytest.raises(ValueError):
        compose_alert_message(language="en", data=make_input(AlertTier.NONE))


def test_llm_may_rephrase_when_required_fields_survive():
    message = compose_alert_message(language="en", data=make_input(), llm=UpperCaseLLM())
    assert message.rephrased is True
    assert message.text.startswith("Friendly note:")


def test_llm_output_dropping_required_fields_is_rejected():
    template = compose_alert_message(language="en", data=make_input())
    message = compose_alert_message(language="en", data=make_input(), llm=FieldDroppingLLM())
    assert message.rephrased is False
    assert message.text == template.text


def test_llm_failure_falls_back_to_template():
    template = compose_alert_message(language="en", data=make_input())
    message = compose_alert_message(language="en", data=make_input(), llm=FailingLLM())
    assert message.rephrased is False
    assert message.text == template.text


def test_message_never_changes_the_tier():
    message = compose_alert_message(language="en", data=make_input(AlertTier.ATTENTION), llm=UpperCaseLLM())
    assert message.tier is AlertTier.ATTENTION


def test_missing_safe_point_is_stated_explicitly():
    message = compose_alert_message(language="en", data=make_input(safe_point=None))
    assert "municipality" in message.text.lower()
    assert "not defined" not in message.text.lower()


# --- language support ---------------------------------------------------------------


class FakeTemplates:
    """Stands in for a human-reviewed template file. The text is a placeholder, NOT real Quechua."""

    def __init__(self, messages=None, language="qu"):
        self._templates = {
            language: ReviewedTemplates(
                language=language,
                reviewed_by="Test Reviewer",
                reviewed_on="2026-01-01",
                messages=messages
                if messages is not None
                else {
                    "EVACUATION": "PLACEHOLDER-QU {site_name} {distance_km} {safe_point} {official_source} {disclaimer}",
                },
            )
        }

    def get(self, language):
        return self._templates.get(language)


def test_household_messages_default_to_spanish():
    message = compose_alert_message(make_input(AlertTier.ATTENTION))
    assert "lluvias" in message.text
    assert "no las reemplaza" in message.text
    assert "SENAMHI" in message.text and "INDECI" in message.text and "ENFEN" in message.text


@pytest.mark.parametrize("tier", [AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION])
def test_spanish_template_contains_all_required_fields(tier):
    message = compose_alert_message(make_input(tier), language="es")
    for needle in ("SYNTHETIC Tailings Alpha", "7.5 km", "Plaza Mayor", "SENAMHI Aviso 123/2026", "SENAMHI", "INDECI", "ENFEN"):
        assert needle in message.text


def test_spanish_evacuation_message_is_imperative_and_formal():
    text = compose_alert_message(make_input(AlertTier.EVACUATION), language="es").text
    assert "EVAC" in text and "punto seguro" in text.lower()
    assert " vos " not in text.lower()


def test_spanish_missing_safe_point_asks_the_municipality_and_never_says_undefined():
    text = compose_alert_message(make_input(safe_point=None), language="es").text
    assert "Consulte a su municipalidad el punto seguro" in text
    assert "no definido" not in text.lower()


def test_operator_message_defaults_to_english_but_can_be_spanish():
    pending = make_input(AlertTier.PENDING_CONFIRMATION)
    assert "confirm" in compose_alert_message(pending).text.lower()
    assert "operador" in compose_alert_message(pending, language="es").text.lower()


def test_english_is_still_available_explicitly():
    assert "does not replace" in compose_alert_message(make_input(), language="en").text


def test_llm_rephrase_receives_the_language_and_is_still_fact_checked():
    seen = []

    class Recording:
        def rephrase(self, text, language="en"):
            seen.append(language)
            return "Nota: " + text

        def explain(self, facts, language="en"):
            return facts

    message = compose_alert_message(make_input(), llm=Recording(), language="es")
    assert seen == ["es"] and message.rephrased is True
    dropped = compose_alert_message(make_input(), llm=FieldDroppingLLM(), language="es")
    assert dropped.rephrased is False


def test_quechua_without_a_reviewed_template_is_not_available_and_never_machine_generated():
    with pytest.raises(TemplateNotAvailable) as error:
        compose_alert_message(make_input(), language="qu")
    assert "human-reviewed" in str(error.value)
    assert error.value.language == "qu"


def test_quechua_hook_requires_a_reviewed_template_for_the_tier():
    templates = FakeTemplates()
    with pytest.raises(TemplateNotAvailable):
        compose_alert_message(make_input(AlertTier.ATTENTION), language="qu", templates=templates)
    with pytest.raises(TemplateNotAvailable):
        compose_alert_message(make_input(), language="qu", templates=FakeTemplates(language="other"))


def test_reviewed_template_is_filled_verbatim_and_the_llm_never_touches_it():
    message = compose_alert_message(make_input(), language="qu", templates=FakeTemplates(), llm=UpperCaseLLM())
    assert message.text.startswith("PLACEHOLDER-QU SYNTHETIC Tailings Alpha 7.5 Plaza Mayor SENAMHI Aviso 123/2026")
    assert message.rephrased is False
    assert message.language == "qu"


def test_reviewed_template_text_with_unknown_placeholders_is_rejected():
    bad = FakeTemplates({"EVACUATION": "PLACEHOLDER {unknown_field}"})
    with pytest.raises(TemplateNotAvailable):
        compose_alert_message(make_input(), language="qu", templates=bad)


def test_message_reports_its_language():
    assert compose_alert_message(make_input()).language == "es"
    assert compose_alert_message(make_input(), language="en").language == "en"


# --- proposed safe points -----------------------------------------------------------

PROPOSED = SafePoint(
    lon=-76.0,
    lat=-12.0,
    elevation_m=140.0,
    distance_m=1230.0,
    bearing_deg=47.0,
    height_above_flow_m=42.0,
    distance_to_flow_m=410.0,
    slope_deg=8.0,
)
PROPOSED_LABEL = "propuesto — validar con la municipalidad"


def test_a_proposed_safe_point_is_described_and_always_labelled_as_a_proposal():
    text = compose_alert_message(make_input(safe_point=None, proposed_safe_point=PROPOSED), language="es").text
    assert "1.2 km al noreste" in text
    assert PROPOSED_LABEL in text
    assert "Consulte a su municipalidad" not in text


def test_a_curated_safe_point_wins_over_a_proposal_and_is_not_labelled_as_proposed():
    text = compose_alert_message(make_input(proposed_safe_point=PROPOSED), language="es").text
    assert "Plaza Mayor" in text and PROPOSED_LABEL not in text


def test_a_proposed_safe_point_in_english_is_labelled_too():
    text = compose_alert_message(make_input(safe_point=None, proposed_safe_point=PROPOSED), language="en").text
    assert "1.2 km to the northeast" in text
    assert "proposed" in text.lower() and "validate" in text.lower()


def test_reviewed_templates_receive_the_proposed_safe_point_text_with_its_label():
    templates = FakeTemplates({"EVACUATION": "X {safe_point}"})
    text = compose_alert_message(
        make_input(safe_point=None, proposed_safe_point=PROPOSED), language="qu", templates=templates
    ).text
    assert PROPOSED_LABEL in text


def test_llm_rephrase_must_keep_the_proposed_safe_point_text():
    class DropsSafePoint:
        def rephrase(self, text, language="en"):
            return "Evacúe. SYNTHETIC Tailings Alpha 7.5 SENAMHI Aviso 123/2026 SENAMHI INDECI ENFEN"

        def explain(self, facts, language="en"):
            return facts

    message = compose_alert_message(
        make_input(safe_point=None, proposed_safe_point=PROPOSED), llm=DropsSafePoint(), language="es"
    )
    assert message.rephrased is False


# --- low-literacy Spanish household messages ----------------------------------------


@pytest.mark.parametrize("tier", [AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION])
def test_spanish_household_message_is_short_and_has_four_plain_lines(tier):
    text = compose_alert_message(make_input(tier), language="es").text
    situation, action, safe_point, source = text.split("\n")
    assert len(text) <= 330
    assert situation.count(".") == 1 and "SYNTHETIC Tailings Alpha" in situation and "7.5 km" in situation
    assert sum(action.count(mark) for mark in ".!?") <= 1  # one short action sentence
    assert safe_point.startswith("Punto seguro:") and "Plaza Mayor" in safe_point
    assert source.startswith("Fuente:") and "SENAMHI Aviso 123/2026" in source
    assert "no las reemplaza" in source


@pytest.mark.parametrize("tier", [AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION])
def test_spanish_household_message_keeps_every_required_fact(tier):
    text = compose_alert_message(make_input(tier, safe_point=None, proposed_safe_point=PROPOSED), language="es").text
    for fact in ("SYNTHETIC Tailings Alpha", "7.5 km", "1.2 km al noreste", PROPOSED_LABEL, "SENAMHI Aviso 123/2026"):
        assert fact in text

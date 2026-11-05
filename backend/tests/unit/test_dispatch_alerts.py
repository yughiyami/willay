"""The alert loop: message every exposed household, call at ATTENTION/EVACUATION, escalate to an operator."""

import pytest

from willay.adapters.alert_ledger import InMemoryAlertLedger
from willay.adapters.fixture_loader import load_rimac_fixture
from willay.adapters.in_memory import InMemorySiteRepository, RecordingMessaging
from willay.adapters.simulated_call import SimulatedCallAdapter
from willay.adapters.web_whatsapp import WebWhatsAppSimulator
from willay.application.dispatch_alerts import (
    DispatchRefused,
    DispatchRequest,
    UnknownAlert,
    dispatch_alerts,
    record_response,
)
from willay.application.rank_sites import rank_sites
from willay.domain.dispatch import Household, ResponseKind
from willay.domain.entities import AlertTier
from willay.ports.calls import CallError, CallNotAllowed, CallResult

FIXTURE = load_rimac_fixture()
REPO = InMemorySiteRepository(FIXTURE.sites, FIXTURE.settlements)
RANKED = {r.site.id: r for r in rank_sites(REPO, FIXTURE.hazard_scenario)}
SITE = RANKED["syn-001"]
REACHED = [c.settlement_id for c in SITE.exposure.contributions]
SOURCE = "SYNTHETIC FIXTURE - not an official bulletin"


def household(n: int, settlement_id: str | None = None, outcome=ResponseKind.NO_ANSWER) -> Household:
    return Household(
        id=f"hh-{n}",
        settlement_id=settlement_id or REACHED[0],
        label=f"Hogar sintetico {n}",
        phone=f"+5190000000{n}",
        demo_call_outcome=outcome,
    )


def request(tier=AlertTier.ATTENTION, households=None, confirmation=None) -> DispatchRequest:
    return DispatchRequest(
        tier=tier,
        site=SITE.site,
        exposure=SITE.exposure,
        settlements=tuple(FIXTURE.settlements),
        households=tuple(households if households is not None else [household(1), household(2), household(3)]),
        official_source=SOURCE,
        operator_confirmation_id=confirmation,
    )


class Setup:
    def __init__(self, calls=None):
        self.messaging = RecordingMessaging()
        self.calls = calls or SimulatedCallAdapter(
            {"+51900000001": ResponseKind.SAFE, "+51900000002": ResponseKind.NEEDS_HELP}
        )
        self.ledger = InMemoryAlertLedger()

    def run(self, req):
        return dispatch_alerts(req, self.messaging, self.calls, self.ledger)


# --- guards ------------------------------------------------------------------------


def test_evacuation_without_an_operator_confirmation_id_is_refused_and_nothing_is_sent():
    setup = Setup()
    for missing in (None, "", "   "):
        with pytest.raises(DispatchRefused):
            setup.run(request(AlertTier.EVACUATION, confirmation=missing))
    assert setup.messaging.sent == [] and setup.calls.calls == [] and setup.ledger.list_alerts() == []


def test_evacuation_with_a_confirmation_id_goes_out_and_the_id_is_kept_for_the_audit_trail():
    setup = Setup()
    result = setup.run(request(AlertTier.EVACUATION, confirmation="OP-2026-0007"))
    assert len(result.alerts) == 3 and result.operator_confirmation_id == "OP-2026-0007"
    assert setup.messaging.sent and setup.calls.calls


@pytest.mark.parametrize("tier", [AlertTier.NONE, AlertTier.PENDING_CONFIRMATION])
def test_only_household_tiers_can_be_dispatched(tier):
    with pytest.raises(DispatchRefused):
        Setup().run(request(tier, confirmation="OP-1"))


# --- who is targeted and what they receive -----------------------------------------


def test_only_households_in_settlements_reached_by_the_flow_path_are_targeted():
    off_river = household(9, settlement_id="set-006")
    assert "set-006" not in REACHED
    setup = Setup()
    result = setup.run(request(households=[household(1), off_river]))
    assert [a.household.id for a in result.alerts] == ["hh-1"]


def test_each_household_gets_the_spanish_composed_message_with_its_own_settlement_facts():
    setup = Setup()
    result = setup.run(request(AlertTier.ATTENTION))
    recipient, text = setup.messaging.sent[0]
    assert recipient == "+51900000001"
    assert SITE.site.name in text and SOURCE in text and "lluvias" in text
    assert result.alerts[0].message_text == text and result.alerts[0].message_status == "sent"


def test_preparation_sends_messages_only_and_places_no_calls():
    setup = Setup()
    result = setup.run(request(AlertTier.PREPARATION))
    assert len(setup.messaging.sent) == 3 and setup.calls.calls == []
    assert all(a.call_status == "not_attempted" and a.response is None for a in result.alerts)
    assert result.cases == []


def test_attention_and_evacuation_also_call_each_household_with_the_tier():
    for tier, confirmation in ((AlertTier.ATTENTION, None), (AlertTier.EVACUATION, "OP-1")):
        setup = Setup()
        setup.run(request(tier, confirmation=confirmation))
        assert setup.calls.calls == [("+51900000001", tier), ("+51900000002", tier), ("+51900000003", tier)]


# --- responses and operator cases ----------------------------------------------------


def test_safe_answers_need_no_case_while_needs_help_and_no_answer_open_one_each():
    setup = Setup()
    result = setup.run(request(AlertTier.ATTENTION))
    by_household = {a.household.id: a for a in result.alerts}
    assert by_household["hh-1"].response is ResponseKind.SAFE and by_household["hh-1"].response_channel == "call"
    assert by_household["hh-2"].response is ResponseKind.NEEDS_HELP
    assert by_household["hh-3"].response is ResponseKind.NO_ANSWER
    reasons = {c.household_id: c.reason for c in result.cases}
    assert reasons == {"hh-2": ResponseKind.NEEDS_HELP, "hh-3": ResponseKind.NO_ANSWER}
    assert all(c.status == "open" and c.site_id == "syn-001" for c in result.cases)
    assert setup.ledger.list_cases() == result.cases


def test_a_number_blocked_by_the_allowlist_is_not_dialled_and_escalates_as_no_answer():
    class Blocking:
        def call(self, number, tier):
            raise CallNotAllowed("not on the allow-list")

    setup = Setup(calls=Blocking())
    result = setup.run(request())
    assert all(a.call_status == "blocked" and a.response is ResponseKind.NO_ANSWER for a in result.alerts)
    assert len(result.cases) == 3 and "allow-list" in result.cases[0].note


def test_a_telephony_failure_escalates_as_no_answer_and_does_not_stop_the_loop():
    class Flaky:
        def __init__(self):
            self.n = 0

        def call(self, number, tier):
            self.n += 1
            if self.n == 1:
                raise CallError("ARI unreachable")
            return CallResult(ResponseKind.SAFE, "key 1")

    result = Setup(calls=Flaky()).run(request())
    assert [a.call_status for a in result.alerts] == ["error", "completed", "completed"]
    assert [a.response for a in result.alerts] == [ResponseKind.NO_ANSWER, ResponseKind.SAFE, ResponseKind.SAFE]
    assert len(result.cases) == 1


def test_a_failing_message_channel_does_not_stop_the_calls():
    class BrokenMessaging:
        def send(self, recipient, text):
            raise RuntimeError("gateway down")

    setup = Setup()
    result = dispatch_alerts(request(), BrokenMessaging(), setup.calls, setup.ledger)
    assert all(a.message_status == "failed" for a in result.alerts)
    assert len(setup.calls.calls) == 3


def test_a_household_language_without_a_reviewed_template_falls_back_to_spanish():
    quechua = Household("hh-q", REACHED[0], "Hogar q", "+51900000007", language="qu")
    setup = Setup()
    result = setup.run(request(households=[quechua]))
    assert result.alerts[0].message_language == "es"


# --- replies from the web simulator --------------------------------------------------


def test_a_later_safe_reply_closes_the_open_case_and_records_the_channel():
    setup = Setup()
    result = setup.run(request())
    alert = next(a for a in result.alerts if a.household.id == "hh-3")
    updated = record_response(alert.id, ResponseKind.SAFE, "whatsapp", setup.ledger)
    assert updated.response is ResponseKind.SAFE and updated.response_channel == "whatsapp"
    case = next(c for c in setup.ledger.list_cases() if c.household_id == "hh-3")
    assert case.status == "closed"


def test_a_needs_help_reply_opens_a_case_once():
    setup = Setup()
    result = setup.run(request(AlertTier.PREPARATION))
    alert = result.alerts[0]
    record_response(alert.id, ResponseKind.NEEDS_HELP, "whatsapp", setup.ledger)
    record_response(alert.id, ResponseKind.NEEDS_HELP, "whatsapp", setup.ledger)
    open_cases = [c for c in setup.ledger.list_cases() if c.status == "open"]
    assert len(open_cases) == 1 and open_cases[0].reason is ResponseKind.NEEDS_HELP


def test_a_reply_to_an_unknown_alert_is_an_error():
    with pytest.raises(UnknownAlert):
        record_response("alert-9999", ResponseKind.SAFE, "whatsapp", InMemoryAlertLedger())


# --- the web WhatsApp-style simulator -------------------------------------------------


def test_the_web_simulator_stores_messages_per_recipient_in_order():
    sim = WebWhatsAppSimulator()
    sim.send("+51900000001", "uno")
    sim.send("+51900000002", "otro")
    sim.send("+51900000001", "dos")
    assert [m.text for m in sim.messages_for("+51900000001")] == ["uno", "dos"]
    assert len(sim.all_messages()) == 3
    assert sim.messages_for("+51900000009") == []


# --- interactive buttons (WhatsApp Cloud API) -----------------------------------------------------------------


class ButtonMessaging:
    """A messaging adapter that also supports reply buttons (InteractiveMessagingPort)."""

    def __init__(self, template_only=False, fail=False):
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list]] = []
        self.template_only, self.fail = template_only, fail

    def send(self, recipient, text):
        self.texts.append((recipient, text))

    def send_buttons(self, recipient, body, buttons):
        from willay.ports.messaging import SendResult

        if self.fail:
            raise RuntimeError("down")
        self.buttons.append((recipient, body, list(buttons)))
        return SendResult(message_id=f"wamid.{len(self.buttons)}", used_template=self.template_only)


def test_a_button_capable_channel_gets_the_alert_with_safe_and_help_buttons_bound_to_the_alert_id():
    setup = Setup()
    setup.messaging = ButtonMessaging()
    result = setup.run(request(AlertTier.PREPARATION, households=[household(1)]))
    alert = result.alerts[0]
    assert setup.messaging.texts == []
    (recipient, body, buttons) = setup.messaging.buttons[0]
    assert recipient == "+51900000001" and body == alert.message_text
    assert buttons == [(f"SAFE:{alert.id}", "Estoy a salvo"), (f"HELP:{alert.id}", "Necesito ayuda")]
    assert alert.message_status == "sent" and alert.message_id == "wamid.1"


def test_a_template_only_delivery_is_not_reported_as_the_alert_being_sent():
    setup = Setup()
    setup.messaging = ButtonMessaging(template_only=True)
    alert = setup.run(request(AlertTier.PREPARATION, households=[household(1)])).alerts[0]
    assert alert.message_status == "template_only" and "24" in alert.message_note


def test_a_failing_button_send_marks_the_alert_failed_and_the_loop_continues():
    setup = Setup()
    setup.messaging = ButtonMessaging(fail=True)
    alerts = setup.run(request(AlertTier.PREPARATION)).alerts
    assert len(alerts) == 3 and all(a.message_status == "failed" for a in alerts)


def test_a_plain_channel_still_gets_a_plain_text_message():
    setup = Setup()
    alert = setup.run(request(AlertTier.PREPARATION, households=[household(1)])).alerts[0]
    assert setup.messaging.sent and alert.message_status == "sent" and alert.message_id is None

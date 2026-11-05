"""Use case: the alert loop.

dispatch_alerts(tier, site) messages every SYNTHETIC demo household downstream of the site (WhatsApp-style, through
MessagingPort) and, at ATTENTION and EVACUATION, also calls it (CallPort: the prerecorded message of the tier,
key 1 = safe, key 2 = needs help). Every answer is recorded; NEEDS_HELP and NO_ANSWER open an operator case.

Guards: only the household tiers can be dispatched, and EVACUATION needs an operator confirmation id (a human
decided; the id is kept for the audit trail). The tier itself is an input decided by the domain rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from willay.application.compose_alert_message import HOUSEHOLD_LANGUAGE, AlertMessageInput, compose_alert_message
from willay.domain.dispatch import Household, HouseholdAlert, OperatorCase, ResponseKind
from willay.domain.entities import AlertTier, Settlement, TailingsSite
from willay.domain.exposure import ExposureResult
from willay.ports.alert_ledger import AlertLedgerPort
from willay.ports.calls import CallError, CallNotAllowed, CallPort
from willay.ports.llm import LLMPort
from willay.ports.messaging import InteractiveMessagingPort, MessagingPort
from willay.ports.templates import ReviewedTemplatesPort, TemplateNotAvailable

HOUSEHOLD_TIERS = (AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION)
CALL_TIERS = (AlertTier.ATTENTION, AlertTier.EVACUATION)
# Reply buttons on channels that support them; the button id carries the alert id so a tap is unambiguous.
SAFE_BUTTON, HELP_BUTTON = ("SAFE", "Estoy a salvo"), ("HELP", "Necesito ayuda")
TEMPLATE_ONLY_NOTE = (
    "outside the 24 h window: only the generic template was delivered, not this alert text "
    "(the household must message the bot first)"
)


class DispatchRefused(ValueError):
    """The dispatch is not allowed (wrong tier, or EVACUATION without an operator confirmation id)."""


class UnknownAlert(KeyError):
    """No dispatched alert has this id."""


@dataclass(frozen=True)
class DispatchRequest:
    tier: AlertTier
    site: TailingsSite
    exposure: ExposureResult | None
    settlements: Sequence[Settlement]
    households: Sequence[Household]
    official_source: str
    operator_confirmation_id: str | None = None
    llm: LLMPort | None = None
    templates: ReviewedTemplatesPort | None = None


@dataclass
class DispatchResult:
    dispatch_id: str
    tier: AlertTier
    site_id: str
    operator_confirmation_id: str | None
    alerts: list[HouseholdAlert] = field(default_factory=list)
    cases: list[OperatorCase] = field(default_factory=list)


def _validate(request: DispatchRequest) -> str | None:
    if request.tier not in HOUSEHOLD_TIERS:
        raise DispatchRefused(f"tier {request.tier.value} cannot be dispatched to households")
    confirmation = (request.operator_confirmation_id or "").strip() or None
    if request.tier is AlertTier.EVACUATION and confirmation is None:
        raise DispatchRefused("an EVACUATION dispatch requires an operator confirmation id")
    return confirmation


def _compose(request: DispatchRequest, household: Household, settlement: Settlement | None, distance_km: float):
    data = AlertMessageInput(
        tier=request.tier,
        site_name=request.site.name,
        distance_km=distance_km,
        safe_point=settlement.safe_point if settlement else None,
        official_source=request.official_source,
        proposed_safe_point=settlement.proposed_safe_point if settlement else None,
    )
    try:
        return compose_alert_message(data, llm=request.llm, language=household.language, templates=request.templates)
    except TemplateNotAvailable:
        # Never machine-generate another language: fall back to the approved Spanish template.
        return compose_alert_message(data, llm=request.llm, language=HOUSEHOLD_LANGUAGE)


def _open_case(
    ledger: AlertLedgerPort, alert: HouseholdAlert, reason: ResponseKind, note: str
) -> OperatorCase:
    case = OperatorCase(
        id=ledger.new_id("case"),
        alert_id=alert.id,
        household_id=alert.household.id,
        site_id=alert.site_id,
        tier=alert.tier,
        reason=reason,
        note=note,
    )
    ledger.add_case(case)
    return case


def _send(messaging: MessagingPort, alert: HouseholdAlert) -> None:
    """Deliver the alert text (with SAFE/HELP reply buttons when the channel supports them) and record the outcome."""
    phone, text = alert.household.phone, alert.message_text
    if not isinstance(messaging, InteractiveMessagingPort):
        messaging.send(phone, text)
        alert.message_status = "sent"
        return
    result = messaging.send_buttons(phone, text, [(f"{prefix}:{alert.id}", title) for prefix, title in (SAFE_BUTTON, HELP_BUTTON)])
    alert.message_id = result.message_id
    if result.used_template:
        alert.message_status, alert.message_note = "template_only", TEMPLATE_ONLY_NOTE
    else:
        alert.message_status = "sent"


def _call(calls: CallPort, alert: HouseholdAlert) -> tuple[ResponseKind, str]:
    """Place the call and return (response, note). Failures to reach the household count as NO_ANSWER."""
    try:
        result = calls.call(alert.household.phone, alert.tier)
    except CallNotAllowed as exc:
        alert.call_status, alert.call_detail = "blocked", str(exc)
        return ResponseKind.NO_ANSWER, f"call not placed: {exc}"
    except CallError as exc:
        alert.call_status, alert.call_detail = "error", str(exc)
        return ResponseKind.NO_ANSWER, f"call failed: {exc}"
    alert.call_status, alert.call_detail = "completed", result.detail
    return result.outcome, f"call result: {result.detail}"


def dispatch_alerts(
    request: DispatchRequest, messaging: MessagingPort, calls: CallPort, ledger: AlertLedgerPort
) -> DispatchResult:
    confirmation = _validate(request)
    dispatch_id = ledger.new_id("dispatch")
    result = DispatchResult(dispatch_id, request.tier, request.site.id, confirmation)

    reached = {c.settlement_id: c for c in (request.exposure.contributions if request.exposure else ())}
    settlements = {s.id: s for s in request.settlements}

    for household in request.households:
        contribution = reached.get(household.settlement_id)
        if contribution is None:
            continue  # not downstream of this site
        settlement = settlements.get(household.settlement_id)
        message = _compose(request, household, settlement, contribution.along_path_m / 1000.0)
        alert = HouseholdAlert(
            id=ledger.new_id("alert"),
            dispatch_id=dispatch_id,
            site_id=request.site.id,
            tier=request.tier,
            household=household,
            settlement_name=contribution.settlement_name,
            lon=contribution.lon,
            lat=contribution.lat,
            message_text=message.text,
            message_language=message.language,
        )
        try:
            _send(messaging, alert)
        except Exception:  # one broken channel must not stop the rest of the loop
            alert.message_status = "failed"

        note = ""
        if request.tier in CALL_TIERS:
            alert.response, note = _call(calls, alert)
            alert.response_channel = "call" if alert.call_status == "completed" else None
        ledger.add_alert(alert)
        result.alerts.append(alert)
        if alert.response is not None and alert.response.needs_operator:
            result.cases.append(_open_case(ledger, alert, alert.response, note))
    return result


def record_response(alert_id: str, response: ResponseKind, channel: str, ledger: AlertLedgerPort) -> HouseholdAlert:
    """A household answered later (for example a WhatsApp reply). SAFE closes its open case; anything else opens one."""
    alert = ledger.get_alert(alert_id)
    if alert is None:
        raise UnknownAlert(alert_id)
    alert.response, alert.response_channel = response, channel
    open_case = ledger.open_case_for(alert.id)
    if response.needs_operator:
        if open_case is None:
            _open_case(ledger, alert, response, f"reported via {channel}")
        else:
            open_case.reason = response
    elif open_case is not None:
        open_case.status = "closed"
        open_case.note = f"{open_case.note}; closed: household reported safe via {channel}".lstrip("; ")
    return alert

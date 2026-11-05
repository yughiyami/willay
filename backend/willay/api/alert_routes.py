"""The alert loop over HTTP: dispatch, operator cases, household statuses and simulator replies.

Only SYNTHETIC demo households are ever contacted, and phone numbers are masked in every response.
"""

from __future__ import annotations

from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from willay.api.dependencies import AppDependencies
from willay.application.dispatch_alerts import (
    DispatchRefused,
    DispatchRequest,
    UnknownAlert,
    dispatch_alerts,
    record_response,
)
from willay.application.rank_sites import RankedSite
from willay.domain.dispatch import HouseholdAlert, OperatorCase, ResponseKind, mask_phone
from willay.domain.entities import AlertTier


class DispatchIn(BaseModel):
    site_id: str = Field(min_length=1, max_length=80)
    tier: Literal["PREPARATION", "ATTENTION", "EVACUATION"] = Field(
        description="Tier from /alerts/evaluate (domain rules). NONE and PENDING_CONFIRMATION cannot be dispatched."
    )
    operator_confirmation_id: str | None = Field(
        default=None, max_length=80, description="Required for EVACUATION: the id of the operator who confirmed it"
    )

    @model_validator(mode="after")
    def _evacuation_needs_a_human(self) -> "DispatchIn":
        if self.tier == "EVACUATION" and not (self.operator_confirmation_id or "").strip():
            raise ValueError("EVACUATION requires operator_confirmation_id")
        return self


class ResponseIn(BaseModel):
    response: Literal["SAFE", "NEEDS_HELP", "NO_ANSWER"]
    channel: Literal["whatsapp", "call"] = "whatsapp"


def alert_to_dict(alert: HouseholdAlert) -> dict:
    return {
        "id": alert.id,
        "dispatch_id": alert.dispatch_id,
        "site_id": alert.site_id,
        "tier": alert.tier.value,
        "household_id": alert.household.id,
        "household_label": alert.household.label,
        "phone_masked": mask_phone(alert.household.phone),
        "settlement_name": alert.settlement_name,
        "lon": alert.lon,
        "lat": alert.lat,
        "message_text": alert.message_text,
        "message_language": alert.message_language,
        "message_status": alert.message_status,
        "message_note": alert.message_note,
        "call_status": alert.call_status,
        "call_detail": alert.call_detail,
        "response": alert.response.value if alert.response else None,
        "response_channel": alert.response_channel,
    }


def case_to_dict(case: OperatorCase, alerts: dict[str, HouseholdAlert]) -> dict:
    alert = alerts.get(case.alert_id)
    return {
        "id": case.id,
        "alert_id": case.alert_id,
        "household_id": case.household_id,
        "household_label": alert.household.label if alert else case.household_id,
        "settlement_name": alert.settlement_name if alert else "",
        "lon": alert.lon if alert else None,
        "lat": alert.lat if alert else None,
        "site_id": case.site_id,
        "tier": case.tier.value,
        "reason": case.reason.value,
        "status": case.status,
        "note": case.note,
        "kind": case.kind,
        "reported_lat": case.lat,
        "reported_lon": case.lon,
        "location_note": case.location_note,
    }


def execute_dispatch(deps: AppDependencies, ranked: RankedSite, tier: AlertTier, operator_confirmation_id: str | None) -> dict:
    """Message (and at ATTENTION/EVACUATION call) the synthetic households downstream of a site.

    Raises DispatchRefused for a tier that cannot be dispatched or an EVACUATION without an operator confirmation id,
    and ValueError when the site has no downstream exposure. Shared by the operator's direct form and by the approval
    of an agent proposal: both are explicit human actions.
    """
    if ranked.exposure is None:
        raise ValueError("this site has no downstream exposure to alert")
    result = dispatch_alerts(
        DispatchRequest(
            tier=tier,
            site=ranked.site,
            exposure=ranked.exposure,
            settlements=tuple(deps.repository.list_settlements()),
            households=tuple(deps.households),
            official_source=deps.official_source,
            operator_confirmation_id=operator_confirmation_id,
            llm=deps.llm,
            templates=deps.reviewed_templates,  # type: ignore[arg-type]
        ),
        deps.messaging,
        deps.calls,
        deps.ledger,
    )
    if deps.alert_fanout is not None:  # web chat and Telegram chats receive the alert too; their failures never break the dispatch
        try:
            deps.alert_fanout(result)
        except Exception:
            pass
    alerts = {a.id: a for a in result.alerts}
    return {
        "dispatch_id": result.dispatch_id,
        "tier": result.tier.value,
        "site_id": result.site_id,
        "operator_confirmation_id": result.operator_confirmation_id,
        "households_targeted": len(result.alerts),
        "synthetic_households": True,
        "calls_mode": deps.calls_mode,
        "alerts": [alert_to_dict(a) for a in result.alerts],
        "cases": [case_to_dict(c, alerts) for c in result.cases],
    }


def register_alert_routes(app: FastAPI, deps: AppDependencies, find_ranked: Callable[[str], RankedSite]) -> None:
    ledger = deps.ledger

    @app.post("/alerts/dispatch")
    def dispatch(request: DispatchIn) -> dict:
        ranked = find_ranked(request.site_id)  # 404 for an unknown site
        try:
            return execute_dispatch(deps, ranked, AlertTier(request.tier), request.operator_confirmation_id)
        except ValueError as refused:  # DispatchRefused is a ValueError
            status = 422 if isinstance(refused, DispatchRefused) else 409
            raise HTTPException(status_code=status, detail=str(refused)) from refused

    @app.get("/alerts/households")
    def households() -> dict:
        alerts = ledger.list_alerts()
        counts: dict[str, int] = {"AWAITING": 0, "SAFE": 0, "NEEDS_HELP": 0, "NO_ANSWER": 0}
        for alert in alerts:
            counts[alert.response.value if alert.response else "AWAITING"] += 1
        return {"synthetic_households": True, "counts": counts, "alerts": [alert_to_dict(a) for a in alerts]}

    @app.get("/alerts/cases")
    def cases(status: Literal["open", "closed"] | None = Query(default=None)) -> dict:
        alerts = {a.id: a for a in ledger.list_alerts()}
        items = [c for c in ledger.list_cases() if status is None or c.status == status]
        return {"cases": [case_to_dict(c, alerts) for c in items]}

    @app.post("/alerts/{alert_id}/response")
    def respond(alert_id: str, request: ResponseIn) -> dict:
        try:
            alert = record_response(alert_id, ResponseKind(request.response), request.channel, ledger)
        except UnknownAlert as unknown:
            raise HTTPException(status_code=404, detail=f"unknown alert '{alert_id}'") from unknown
        alerts = {a.id: a for a in ledger.list_alerts()}
        related = [case_to_dict(c, alerts) for c in ledger.list_cases() if c.alert_id == alert.id]
        return {"alert": alert_to_dict(alert), "cases": related}

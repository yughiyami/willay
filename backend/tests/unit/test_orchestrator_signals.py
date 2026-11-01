"""Public signals inside the orchestrator: the HazardAnalyst tool and, above all, the guardrails (context only, never a trigger)."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tests.unit.orchestration_fixtures import (
    ATTENTION_SCENARIO,
    RAIN_SCENARIO,
    SPARK,
    WONDER,
    build_orchestrator,
    build_services,
    call,
    run,
    turn,
)
from willay.application.orchestration.orchestrator import AGENT_TOOLS, all_tool_names
from willay.application.public_signals import PublicSignalsHub
from willay.domain.entities import HazardContext
from willay.domain.signals import SIGNAL_LABEL_ES, PublicSignal, SignalEventType

NOW = datetime(2026, 2, 10, 15, 0, tzinfo=timezone.utc)
SITE = "syn-002"  # Matucana


def matucana_signal(n: int, **overrides) -> PublicSignal:
    values = dict(
        event_type=SignalEventType.HUAICO, place="MATUCANA", ubigeo="150701", district="MATUCANA", lon=-76.38, lat=-11.85,
        event_time=NOW - timedelta(hours=n), confidence=0.8, source_url=f"https://example.org/simulado/{n}", source_domain="example.org",
    )
    values.update(overrides)
    return PublicSignal(**values)


def hub_with(count: int) -> PublicSignalsHub:
    hub = PublicSignalsHub(enabled=True, clock=lambda: NOW)
    hub.replace([matucana_signal(n + 1) for n in range(count)], refreshed_at=NOW)
    return hub


def tool_outputs(fake, name: str) -> list[dict]:
    found = []
    for request in fake.requests:
        for item in request["input_items"]:
            if item.get("type") == "function_call_output" and item["call_id"] == f"call-{name}":
                found.append(json.loads(item["output"]))
    return found


def ask_hazard(*calls):
    """The planner delegates once; the analyst makes `calls`; both then answer."""
    return {
        WONDER: [turn("", call("ask_hazard_analyst", task="senales"), model=WONDER), turn("Listo.", model=WONDER)],
        SPARK: [turn("", *calls), turn("Revisado.")],
    }


def ask_dispatch(*calls):
    return {
        WONDER: [turn("", call("ask_dispatcher", task="p", site_id=SITE), model=WONDER), turn("Listo.", model=WONDER)],
        SPARK: [turn("", *calls), turn("Hecho.")],
    }


# --- the tool -------------------------------------------------------------------------------------------


def test_get_public_signals_belongs_to_the_hazard_analyst_only():
    assert "get_public_signals" in all_tool_names()
    owners = [agent for agent, tools in AGENT_TOOLS.items() if any(t["name"] == "get_public_signals" for t in tools)]
    assert owners == ["hazard_analyst"]


def test_a_district_query_returns_pages_domains_and_times_with_the_unverified_label_and_no_text():
    orchestrator, fake, _ = build_orchestrator(ask_hazard(call("get_public_signals", district="Matucana")), build_services(signals=hub_with(3)))
    run(orchestrator)
    (out,) = tool_outputs(fake, "get_public_signals")
    assert out["available"] is True and out["mode"] == "live" and out["label"] == SIGNAL_LABEL_ES
    (district,) = out["districts"]
    assert district["district"] == "MATUCANA" and district["count"] == 3
    assert {s["url"] for s in district["sources"]} == {f"https://example.org/simulado/{n}" for n in (1, 2, 3)}
    assert all(set(s) == {"url", "domain", "event_type", "event_time"} for s in district["sources"])
    assert "cannot change an alert tier" in out["note"]


def test_a_site_query_reports_the_district_count_and_whether_it_corroborates_attention():
    orchestrator, fake, _ = build_orchestrator(ask_hazard(call("get_public_signals", site_id=SITE)), build_services(signals=hub_with(2)))
    run(orchestrator)
    (out,) = tool_outputs(fake, "get_public_signals")
    assert out["site_id"] == SITE and out["pages_for_site"] == 2 and out["corroborates_attention"] is True
    solo, fake2 = build_orchestrator(ask_hazard(call("get_public_signals", site_id=SITE)), build_services(signals=hub_with(1)))[:2]
    run(solo)
    assert tool_outputs(fake2, "get_public_signals")[0]["corroborates_attention"] is False


def test_unknown_site_and_no_arguments_are_handled():
    orchestrator, fake, _ = build_orchestrator(
        ask_hazard(call("get_public_signals", site_id="nope")), build_services(signals=hub_with(1))
    )
    run(orchestrator)
    assert "error" in tool_outputs(fake, "get_public_signals")[0]
    overview, fake2, _ = build_orchestrator(ask_hazard(call("get_public_signals")), build_services(signals=hub_with(2)))
    run(overview)
    assert tool_outputs(fake2, "get_public_signals")[0]["districts"][0]["count"] == 2


def test_with_signals_off_the_tool_says_so_instead_of_inventing():
    for services in (build_services(), build_services(signals=PublicSignalsHub(clock=lambda: NOW))):
        orchestrator, fake, _ = build_orchestrator(ask_hazard(call("get_public_signals", district="Matucana")), services)
        result, trace = run(orchestrator)
        (out,) = tool_outputs(fake, "get_public_signals")
        assert out["available"] is False and out["mode"] == "off" and out["districts"] == []
        assert "not enabled" in out["note"]
    summary = [e["result_summary"] for e in trace.snapshot()["events"] if e["kind"] == "tool_call" and e["tool"] == "get_public_signals"]
    assert summary and "mode=off" in summary[0]


# --- guardrails ---------------------------------------------------------------------------------------------


def test_signals_alone_never_create_a_tier_or_a_dispatch_proposal():
    services = build_services(signals=hub_with(500))
    orchestrator, fake, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_dispatcher", task="p", site_id=SITE), model=WONDER), turn("Nada que enviar.", model=WONDER)],
            SPARK: [turn("", call("evaluate_alert", site_id=SITE), call("prepare_dispatch", site_id=SITE)), turn("No aplica.")],
        },
        services,
    )
    result, _ = run(orchestrator, scenario=HazardContext())
    (evaluation,) = tool_outputs(fake, "evaluate_alert")
    assert evaluation["tier"] == "NONE" and evaluation["reasons"] == []
    (prepared,) = tool_outputs(fake, "prepare_dispatch")
    assert prepared["proposal"] is None
    assert services.proposals.list() == [] and result.proposals == ()


def test_attention_gets_the_corroboration_reason_and_keeps_its_tier_and_its_proposal_tier():
    with_signals = build_services(signals=hub_with(3))
    orchestrator, fake, _ = build_orchestrator(ask_dispatch(call("evaluate_alert", site_id=SITE), call("prepare_dispatch", site_id=SITE)), with_signals)
    run(orchestrator, scenario=ATTENTION_SCENARIO)
    (evaluation,) = tool_outputs(fake, "evaluate_alert")
    assert evaluation["tier"] == "ATTENTION"
    assert any("corroborated by 3 public signals" in r for r in evaluation["reasons"])
    (proposal,) = with_signals.proposals.list()
    assert proposal.tier.value == "ATTENTION" and proposal.evaluated_tier == "ATTENTION" and proposal.status == "awaiting_operator"
    assert any("corroborated by 3 public signals" in r for r in proposal.reasons)

    plain = build_services()
    orchestrator2, fake2, _ = build_orchestrator(ask_dispatch(call("evaluate_alert", site_id=SITE), call("prepare_dispatch", site_id=SITE)), plain)
    run(orchestrator2, scenario=ATTENTION_SCENARIO)
    baseline = tool_outputs(fake2, "evaluate_alert")[0]
    assert baseline["tier"] == evaluation["tier"] and baseline["triggered"] == evaluation["triggered"]
    assert not any("public signals" in r for r in baseline["reasons"])
    assert plain.proposals.list()[0].tier == proposal.tier


def test_a_single_page_is_not_enough_to_add_the_reason():
    orchestrator, fake, _ = build_orchestrator(ask_dispatch(call("evaluate_alert", site_id=SITE)), build_services(signals=hub_with(1)))
    run(orchestrator, scenario=ATTENTION_SCENARIO)
    assert not any("public signals" in r for r in tool_outputs(fake, "evaluate_alert")[0]["reasons"])


def test_signals_do_not_move_pending_confirmation_or_evacuation():
    scenario = replace(RAIN_SCENARIO, watcher_report=True)
    outcomes = []
    for services in (build_services(), build_services(signals=hub_with(500))):
        orchestrator, fake, _ = build_orchestrator(ask_dispatch(call("evaluate_alert", site_id=SITE), call("prepare_dispatch", site_id=SITE)), services)
        run(orchestrator, scenario=scenario)
        (evaluation,) = tool_outputs(fake, "evaluate_alert")
        (proposal,) = services.proposals.list()
        outcomes.append((evaluation["tier"], evaluation["requires_operator_confirmation"], proposal.tier.value, proposal.evaluated_tier, proposal.status))
        assert not any("public signals" in r for r in evaluation["reasons"])
    assert outcomes[0] == outcomes[1] == ("PENDING_CONFIRMATION", True, "EVACUATION", "PENDING_CONFIRMATION", "awaiting_operator")


def keys_of(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


def test_the_tool_result_never_offers_a_way_to_send_approve_or_confirm():
    orchestrator, fake, _ = build_orchestrator(ask_hazard(call("get_public_signals", site_id=SITE)), build_services(signals=hub_with(5)))
    run(orchestrator)
    keys = keys_of(tool_outputs(fake, "get_public_signals")[0])
    assert not [k for k in keys if any(word in k for word in ("send", "approve", "confirm", "dispatch", "proposal", "tier"))]

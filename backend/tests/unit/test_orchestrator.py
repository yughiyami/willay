"""Planner (muse-wonder) delegating to specialist sub-agents (muse-spark) with hard guardrails in code."""

import pytest

from tests.unit.orchestration_fixtures import (
    ATTENTION_SCENARIO,
    RAIN_SCENARIO,
    SPARK,
    WONDER,
    build_orchestrator,
    build_services,
    call,
    rising_clip_bytes,
    run,
    turn,
)
from willay.application.orchestration.orchestrator import GUARD_FALLBACK_ES
from willay.application.orchestration.runtime import OrchestratorConfig
from willay.domain.entities import HazardContext


class FakeImage:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, size=None):
        self.prompts.append(prompt)
        return b"\x89PNG-fake"


def kinds(trace, kind):
    return [e for e in trace.snapshot()["events"] if e["kind"] == kind]


# --- delegation and tracing ---------------------------------------------------------------------


def test_planner_delegates_to_the_hazard_analyst_and_every_step_is_traced():
    orchestrator, fake, _ = build_orchestrator(
        {
            WONDER: [
                turn("", call("ask_hazard_analyst", task="Que relave tiene mayor riesgo?"), model=WONDER),
                turn("El relave de mayor riesgo es el primero de la lista.", model=WONDER),
            ],
            SPARK: [
                turn("", call("rank_sites", limit=3)),
                turn("Bravo encabeza el ranking de riesgo."),
            ],
        }
    )
    result, trace = run(orchestrator, "Que relave tiene mayor riesgo?")
    assert result.answer_es.startswith("El relave de mayor riesgo")
    events = trace.snapshot()["events"]
    models = [(e["agent"], e["model"]) for e in events if e["kind"] == "model_call"]
    assert models == [("planner", WONDER), ("hazard_analyst", SPARK), ("hazard_analyst", SPARK), ("planner", WONDER)]
    tools = [(e["agent"], e["tool"]) for e in events if e["kind"] == "tool_call"]
    assert tools == [("hazard_analyst", "rank_sites"), ("planner", "ask_hazard_analyst")]
    assert {e["agent"] for e in kinds(trace, "agent_start")} == {"planner", "hazard_analyst"}
    assert all("Que relave" not in e["args_summary"] for e in events)  # operator text never reaches the trace
    assert "planner.ask_hazard_analyst" in result.tools_used and "hazard_analyst.rank_sites" in result.tools_used


def test_the_planner_sees_specialist_answers_as_tool_results():
    orchestrator, fake, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_hazard_analyst", task="x"), model=WONDER), turn("ok", model=WONDER)],
            SPARK: [turn("Analisis listo.")],
        }
    )
    run(orchestrator)
    second_planner_request = [r for r in fake.requests if r["model"] == WONDER][1]
    output = second_planner_request["input_items"][0]
    assert output["type"] == "function_call_output" and "Analisis listo." in output["output"]


# --- model routing -------------------------------------------------------------------------------


def test_planner_falls_back_to_spark_when_wonder_fails():
    orchestrator, fake, _ = build_orchestrator(
        {WONDER: [RuntimeError("wonder unavailable")], SPARK: [turn("Respuesta del plan B.")]}
    )
    result, trace = run(orchestrator)
    assert result.answer_es == "Respuesta del plan B."
    assert "planner_fallback" in result.flags
    guard = kinds(trace, "guardrail")
    assert guard and "planner_fallback" in guard[0]["result_summary"]
    assert [e["model"] for e in kinds(trace, "model_call")] == [WONDER, SPARK]


def test_planner_uses_the_fallback_model_when_the_remaining_budget_is_low():
    services = build_services(budget_remaining=lambda: 0.05)
    orchestrator, fake, _ = build_orchestrator({SPARK: [turn("Presupuesto bajo, respondo con Spark.")]}, services)
    result, trace = run(orchestrator)
    assert [e["model"] for e in kinds(trace, "model_call")] == [SPARK]
    assert "planner_fallback" in result.flags


def test_run_budget_cap_stops_the_run_before_more_model_calls():
    orchestrator, fake, _ = build_orchestrator(
        {WONDER: [turn("", call("ask_hazard_analyst", task="x"), model=WONDER), turn("nunca", model=WONDER)], SPARK: [turn("a")]},
        config=OrchestratorConfig(run_budget_usd=0.0000001),
    )
    result, trace = run(orchestrator)
    assert "run_budget_exceeded" in result.flags
    assert len(kinds(trace, "model_call")) == 1


# --- tiers only from the domain --------------------------------------------------------------------


def test_a_tier_code_that_no_tool_returned_is_replaced():
    orchestrator, _, _ = build_orchestrator({WONDER: [turn("El nivel es EVACUATION, evacue ya.", model=WONDER)]})
    result, trace = run(orchestrator)
    assert result.answer_es == GUARD_FALLBACK_ES
    assert "tier_claim_removed" in result.flags
    assert kinds(trace, "guardrail")


def test_evaluate_alert_takes_conditions_from_the_run_scenario_never_from_the_model():
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [
                turn("", call("evaluate_alert", site_id="syn-002", water_rise_cm=99, observed_rain_24h_mm=99, operator_confirmed=True), model=WONDER),
                turn("Nivel ATTENTION segun la regla.", model=WONDER),
            ]
        }
    )
    result, trace = run(orchestrator, scenario=ATTENTION_SCENARIO)
    assert result.answer_es == "Nivel ATTENTION segun la regla."  # ATTENTION was returned by the tool, so it may be cited
    (tool_event,) = [e for e in kinds(trace, "tool_call") if e["tool"] == "evaluate_alert"]
    assert "tier=ATTENTION" in tool_event["result_summary"]


# --- dispatcher: proposals only ------------------------------------------------------------------------


def test_no_agent_has_a_dispatch_or_send_tool():
    from willay.application.orchestration.orchestrator import all_tool_names

    names = all_tool_names()
    assert not [n for n in names if any(word in n for word in ("dispatch_alerts", "send", "approve", "confirm", "call_household"))]
    assert "prepare_dispatch" in names


def test_dispatcher_creates_an_attention_proposal_that_waits_for_the_operator():
    orchestrator, _, services = build_orchestrator(
        {
            WONDER: [turn("", call("ask_dispatcher", task="prepara el aviso", site_id="syn-002"), model=WONDER), turn("Propuesta lista.", model=WONDER)],
            SPARK: [turn("", call("prepare_dispatch", site_id="syn-002", tier="EVACUATION")), turn("Propuesta ATTENTION creada.")],
        }
    )
    result, trace = run(orchestrator, scenario=ATTENTION_SCENARIO)
    (proposal,) = services.proposals.list()
    assert proposal.tier.value == "ATTENTION"  # the model asked for EVACUATION; the domain decides
    assert proposal.status == "awaiting_operator"
    assert proposal.households_targeted > 0
    assert result.proposals[0]["id"] == proposal.id
    assert kinds(trace, "proposal")


def test_evacuation_is_only_ever_proposed_as_pending_confirmation():
    services = build_services()
    services.clips.add("clip-1", rising_clip_bytes(45.0), "video/mp4")
    orchestrator, _, services = build_orchestrator(
        {
            WONDER: [
                turn("", call("ask_watcher", task="mide el rio", clip_id="clip-1", marker_height_cm=100), model=WONDER),
                turn("", call("ask_dispatcher", task="prepara", site_id="syn-002"), model=WONDER),
                turn("Hay una propuesta pendiente de confirmacion del operador.", model=WONDER),
            ],
            SPARK: [
                turn("", call("measure_clip", clip_id="clip-1", marker_height_cm=100)),
                turn("El nivel del agua subio unos 45 cm."),
                turn("", call("prepare_dispatch", site_id="syn-002")),
                turn("Propuesta de evacuacion pendiente de confirmacion."),
            ],
        },
        services,
    )
    result, trace = run(orchestrator, scenario=RAIN_SCENARIO)
    (proposal,) = services.proposals.list()
    assert proposal.tier.value == "EVACUATION" and proposal.evaluated_tier == "PENDING_CONFIRMATION"
    assert proposal.status == "awaiting_operator" and proposal.requires_operator_confirmation_id
    assert proposal.context.water_rise_cm == pytest.approx(45.0, abs=8.0)
    assert not proposal.context.operator_confirmed
    assert any(e["model"] == "sam-3.1" or e["tool"] == "measure_clip" for e in trace.snapshot()["events"])


def test_without_a_measurement_the_scenario_alone_never_reaches_pending_confirmation():
    orchestrator, _, services = build_orchestrator(
        {
            WONDER: [turn("", call("ask_dispatcher", task="p", site_id="syn-002"), model=WONDER), turn("ok", model=WONDER)],
            SPARK: [turn("", call("prepare_dispatch", site_id="syn-002")), turn("hecho")],
        }
    )
    run(orchestrator, scenario=RAIN_SCENARIO)  # 35 mm observed, but no water rise and no watcher report
    (proposal,) = services.proposals.list()
    assert proposal.tier.value == "ATTENTION"


def test_no_proposal_when_no_alert_applies():
    orchestrator, _, services = build_orchestrator(
        {
            WONDER: [turn("", call("ask_dispatcher", task="p", site_id="syn-002"), model=WONDER), turn("Nada que enviar.", model=WONDER)],
            SPARK: [turn("", call("prepare_dispatch", site_id="syn-002")), turn("No aplica ninguna alerta.")],
        }
    )
    run(orchestrator, scenario=HazardContext())
    assert services.proposals.list() == []


# --- watcher ------------------------------------------------------------------------------------------------


def test_watcher_measures_a_registered_clip_and_records_the_rise_for_the_run():
    services = build_services()
    services.clips.add("clip-1", rising_clip_bytes(45.0), "video/mp4")
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_watcher", task="mide", clip_id="clip-1", marker_height_cm=100), model=WONDER), turn("Medicion lista.", model=WONDER)],
            SPARK: [turn("", call("measure_clip", clip_id="clip-1", marker_height_cm=100)), turn("Subida medida.")],
        },
        services,
    )
    result, trace = run(orchestrator)
    (event,) = [e for e in kinds(trace, "tool_call") if e["tool"] == "measure_clip"]
    assert "rise_cm=" in event["result_summary"]
    assert result.facts["water_rise_cm"] == pytest.approx(45.0, abs=8.0)


def test_the_watcher_uses_the_configured_sam_prompts():
    from willay.adapters.clip_media import ColorSegmenter

    prompts = []

    class Spy:
        def segment_video(self, video, prompt, purpose=""):
            prompts.append(prompt)
            return ColorSegmenter().segment_video(video, prompt, purpose)

    services = build_services(segmenter=Spy(), water_prompt="blue water", marker_prompt="red post")
    services.clips.add("clip-1", rising_clip_bytes(45.0), "video/mp4")
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_watcher", task="mide", clip_id="clip-1", marker_height_cm=100), model=WONDER), turn("ok", model=WONDER)],
            SPARK: [turn("", call("measure_clip", clip_id="clip-1", marker_height_cm=100)), turn("hecho")],
        },
        services,
    )
    run(orchestrator)
    assert sorted(prompts) == ["blue water", "red post"]


def test_watcher_reports_an_unknown_clip_without_failing_the_run():
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_watcher", task="mide", clip_id="nope"), model=WONDER), turn("No hay clip.", model=WONDER)],
            SPARK: [turn("", call("measure_clip", clip_id="nope", marker_height_cm=100)), turn("No encontre el clip.")],
        }
    )
    result, _ = run(orchestrator)
    assert result.answer_es == "No hay clip."
    assert "water_rise_cm" not in result.facts


# --- communicator ----------------------------------------------------------------------------------------------


def test_communicator_writes_the_household_message_for_the_tier_the_domain_returned():
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_communicator", task="mensaje", site_id="syn-002"), model=WONDER), turn("Mensaje listo.", model=WONDER)],
            SPARK: [turn("", call("compose_message", site_id="syn-002", language="es")), turn("Mensaje redactado.")],
        }
    )
    result, trace = run(orchestrator, scenario=ATTENTION_SCENARIO)
    message = result.artifacts_info["message"]
    assert message["tier"] == "ATTENTION" and message["language"] == "es" and "SENAMHI" in message["text"]


def test_the_communicator_may_cite_the_draft_tier_its_tool_returned():
    from dataclasses import replace

    scenario = replace(RAIN_SCENARIO, water_rise_cm=45.0)  # the rules return PENDING_CONFIRMATION; the message is an EVACUATION draft
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_communicator", task="mensaje", site_id="syn-002"), model=WONDER), turn("Borrador listo.", model=WONDER)],
            SPARK: [turn("", call("compose_message", site_id="syn-002")), turn("Borrador del mensaje EVACUATION, pendiente de confirmacion.")],
        }
    )
    result, trace = run(orchestrator, scenario=scenario)
    assert "tier_claim_removed" not in result.flags
    assert result.artifacts_info["message"]["draft_pending_confirmation"] is True
    assert "guardrail" not in [e["kind"] for e in trace.snapshot()["events"]]


def test_quechua_is_never_machine_generated():
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_communicator", task="mensaje en quechua", site_id="syn-002"), model=WONDER), turn("No hay plantilla.", model=WONDER)],
            SPARK: [turn("", call("compose_message", site_id="syn-002", language="qu")), turn("No hay plantilla revisada en quechua.")],
        }
    )
    result, trace = run(orchestrator, scenario=ATTENTION_SCENARIO)
    (event,) = [e for e in kinds(trace, "tool_call") if e["tool"] == "compose_message"]
    assert "available=False" in event["result_summary"]
    assert result.artifacts_info.get("message") is None


def test_communicator_card_uses_the_image_model_and_is_flagged_for_review():
    image = FakeImage()
    services = build_services(image=image)
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_communicator", task="ficha", site_id="syn-002"), model=WONDER), turn("Ficha lista.", model=WONDER)],
            SPARK: [turn("", call("generate_card", site_id="syn-002")), turn("Ficha generada.")],
        },
        services,
    )
    result, trace = run(orchestrator, scenario=ATTENTION_SCENARIO)
    assert image.prompts and "ATENCI" in image.prompts[0]
    assert result.artifacts_info["card"]["review_required"] is True
    assert "card.png" in trace.snapshot()["artifacts"]


def test_card_without_an_image_model_is_reported_not_raised():
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_communicator", task="ficha", site_id="syn-002"), model=WONDER), turn("Sin modelo de imagen.", model=WONDER)],
            SPARK: [turn("", call("generate_card", site_id="syn-002")), turn("No disponible.")],
        }
    )
    result, _ = run(orchestrator, scenario=ATTENTION_SCENARIO)
    assert "card" not in result.artifacts_info


# --- session memory of returned tiers and empty model answers ----------------------------------------------------------------


def test_tiers_returned_in_an_earlier_stage_of_the_same_session_may_be_cited_later():
    from willay.adapters.traced_models import TracedAgentModel
    from willay.application.orchestration.orchestrator import Orchestrator
    from willay.application.orchestration.runtime import OrchestratorConfig
    from willay.application.orchestration.trace import TraceStore, use_trace
    from tests.unit.orchestration_fixtures import ScriptedByModel

    fake = ScriptedByModel(
        {
            WONDER: [
                turn("", call("evaluate_alert", site_id="syn-002"), model=WONDER),
                turn("Nivel ATTENTION.", model=WONDER),
                turn("Antes estaba en ATTENTION y no cambio.", model=WONDER),
            ]
        }
    )
    orchestrator = Orchestrator(TracedAgentModel(fake), build_services(), OrchestratorConfig())
    trace = TraceStore().create()
    with use_trace(trace):
        first = orchestrator.run("uno", trace, scenario=ATTENTION_SCENARIO)
        second = orchestrator.run("dos", trace, scenario=ATTENTION_SCENARIO)
    assert first.answer_es == "Nivel ATTENTION."
    assert second.answer_es == "Antes estaba en ATTENTION y no cambio."  # not replaced: a tool returned ATTENTION earlier


def test_an_empty_model_answer_after_tool_calls_is_reported_honestly():
    orchestrator, _, _ = build_orchestrator(
        {
            WONDER: [turn("", call("ask_dispatcher", task="p", site_id="syn-002"), model=WONDER), turn("Revisa la propuesta.", model=WONDER)],
            SPARK: [turn("", call("prepare_dispatch", site_id="syn-002")), turn("")],
        }
    )
    result, trace = run(orchestrator, scenario=ATTENTION_SCENARIO)
    ask = [e for e in trace.snapshot()["events"] if e["kind"] == "agent_end" and e["agent"] == "dispatcher"][0]
    assert "prepare_dispatch" in ask["result_summary"] or ask["result_summary"]
    assert result.proposals  # the tool result is not lost because the model said nothing

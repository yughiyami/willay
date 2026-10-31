"""Operator agent: tools over our use cases, ui_actions, the tool-calling loop and the tier guard."""

import json

import pytest

from willay.adapters.in_memory import InMemorySiteRepository
from willay.application.operator_agent import (
    OperatorAgent,
    OperatorToolkit,
    TOOL_DEFINITIONS,
    guard_tier_claims,
)
from willay.application.rank_sites import rank_sites
from willay.domain.entities import AlertTier, EnfenMagnitude, HazardContext, RainfallAnomaly, SenamhiLevel
from willay.ports.agent import AgentTurn, ToolCall
from tests.unit.agent_fixtures import DISTRICTS, synthetic_toolkit_parts


@pytest.fixture()
def toolkit() -> OperatorToolkit:
    repository, hazard = synthetic_toolkit_parts()
    ranked = rank_sites(repository, hazard)
    return OperatorToolkit(lambda: ranked, hazard, districts=DISTRICTS)


# --- tool definitions --------------------------------------------------------------------------------


def test_tool_definitions_use_the_flat_responses_format_and_cover_the_required_tools():
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {"rank_sites", "get_site", "get_district", "evaluate_alert", "show_layer", "fly_to", "explain_factors"}
    for tool in TOOL_DEFINITIONS:
        assert tool["type"] == "function" and tool["parameters"]["type"] == "object"
        assert tool["description"]


def test_the_agent_cannot_confirm_an_evacuation():
    evaluate = next(t for t in TOOL_DEFINITIONS if t["name"] == "evaluate_alert")
    assert "operator_confirmed" not in evaluate["parameters"]["properties"]


# --- tools ---------------------------------------------------------------------------------------------


def test_rank_sites_returns_the_top_sites_in_rank_order(toolkit):
    result, actions = toolkit.call("rank_sites", {"limit": 3})
    assert [s["rank"] for s in result["sites"]] == [1, 2, 3]
    assert {"id", "name", "district", "risk", "static_risk"} <= set(result["sites"][0])
    assert actions == []


def test_get_site_returns_detail_and_unknown_site_is_an_error_not_an_exception(toolkit):
    result, _ = toolkit.call("get_site", {"site_id": "syn-001"})
    assert result["id"] == "syn-001" and result["settlements_reached"] is not None
    error, _ = toolkit.call("get_site", {"site_id": "nope"})
    assert "error" in error


def test_get_district_matches_by_name_case_insensitively(toolkit):
    result, _ = toolkit.call("get_district", {"district": "san mateo"})
    assert result["ubigeo"] == "150722" and result["static_score"] == pytest.approx(0.8)
    missing, _ = toolkit.call("get_district", {"district": "atlantis"})
    assert "error" in missing


def test_get_district_without_district_data_says_so():
    repository, hazard = synthetic_toolkit_parts()
    kit = OperatorToolkit(lambda: rank_sites(repository, hazard), hazard, districts=None)
    result, _ = kit.call("get_district", {"district": "San Mateo"})
    assert "error" in result


def test_evaluate_alert_tier_comes_from_the_domain_rules(toolkit):
    calm, _ = toolkit.call("evaluate_alert", {"site_id": "syn-001"})
    storm, _ = toolkit.call(
        "evaluate_alert", {"site_id": "syn-001", "observed_rain_24h_mm": 40, "water_rise_cm": 50}
    )
    assert calm["tier"] in {t.value for t in AlertTier}
    # Evacuation conditions without operator confirmation can only reach PENDING_CONFIRMATION.
    assert storm["tier"] == AlertTier.PENDING_CONFIRMATION.value
    assert storm["requires_operator_confirmation"] is True
    assert storm["inputs"]["water_rise_cm"] == 50


def test_evaluate_alert_ignores_an_attempt_to_pass_operator_confirmation(toolkit):
    result, _ = toolkit.call(
        "evaluate_alert",
        {"site_id": "syn-001", "observed_rain_24h_mm": 40, "water_rise_cm": 50, "operator_confirmed": True},
    )
    assert result["tier"] == AlertTier.PENDING_CONFIRMATION.value


def test_evaluate_alert_for_unknown_site_is_an_error(toolkit):
    result, _ = toolkit.call("evaluate_alert", {"site_id": "nope"})
    assert "error" in result


def test_show_layer_emits_a_toggle_action(toolkit):
    result, actions = toolkit.call("show_layer", {"layer": "districts", "visible": True})
    assert result["ok"] is True
    assert actions == [{"type": "toggle_layer", "layer": "districts", "visible": True}]


def test_show_layer_rejects_unknown_layers(toolkit):
    result, actions = toolkit.call("show_layer", {"layer": "satellite_war", "visible": True})
    assert "error" in result and actions == []


def test_fly_to_site_flies_highlights_the_site_and_its_flow_path(toolkit):
    result, actions = toolkit.call("fly_to", {"site_id": "syn-003"})
    assert result["ok"] is True
    kinds = [a["type"] for a in actions]
    assert kinds == ["fly_to", "highlight_site", "highlight_flow_path"]
    fly = actions[0]
    assert (fly["lon"], fly["lat"]) == (-76.688, -11.931)
    assert fly["height_m"] == 8000.0
    assert actions[1]["site_id"] == "syn-003" and actions[2]["site_id"] == "syn-003"


def test_fly_to_district_uses_the_polygon_center(toolkit):
    _, actions = toolkit.call("fly_to", {"district": "San Mateo"})
    assert actions[0]["type"] == "fly_to"
    assert actions[0]["lon"] == pytest.approx(-76.5) and actions[0]["lat"] == pytest.approx(-11.8)
    assert actions[0]["height_m"] == 40000.0  # wider framing than a single site (8 km)
    assert len(actions) == 1


def test_fly_to_coordinates(toolkit):
    result, actions = toolkit.call("fly_to", {"lon": -76.9, "lat": -12.0})
    assert result["ok"] is True and actions == [{"type": "fly_to", "lon": -76.9, "lat": -12.0, "height_m": 20000.0, "label": None}]


@pytest.mark.parametrize("args", [{}, {"lon": -76.9}, {"lon": 500, "lat": 0}, {"site_id": "nope"}])
def test_fly_to_with_bad_targets_is_an_error(toolkit, args):
    result, actions = toolkit.call("fly_to", args)
    assert "error" in result and actions == []


def test_explain_factors_returns_the_numbers_the_model_may_use(toolkit):
    result, _ = toolkit.call("explain_factors", {"site_id": "syn-001"})
    assert set(result["factors"]) == {"trigger", "susceptibility", "tailings_vulnerability", "downstream_exposure"}
    assert 0.0 <= result["risk"] <= 1.0
    assert result["factors"]["trigger"]["value"] is not None


def test_unknown_tool_is_an_error(toolkit):
    result, _ = toolkit.call("launch_missiles", {})
    assert "error" in result


# --- agent loop ----------------------------------------------------------------------------------------


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def run_turn(self, **kwargs):
        self.requests.append(kwargs)
        return self.turns.pop(0)


def test_agent_runs_the_tool_loop_and_collects_ui_actions(toolkit):
    model = ScriptedModel(
        [
            AgentTurn("r1", "", (ToolCall("c1", "fly_to", {"site_id": "syn-001"}), ToolCall("c2", "show_layer", {"layer": "flowpaths", "visible": True}))),
            AgentTurn("r2", "Volando al relave Alpha y mostrando las rutas de flujo."),
        ]
    )
    reply = OperatorAgent(model, toolkit).answer("llévame al relave Alpha")
    assert reply.answer_es == "Volando al relave Alpha y mostrando las rutas de flujo."
    assert [a["type"] for a in reply.ui_actions] == ["fly_to", "highlight_site", "highlight_flow_path", "toggle_layer"]
    assert reply.response_id == "r2"
    first, second = model.requests
    assert "español" in first["instructions"].lower()
    assert first["previous_response_id"] is None
    assert first["input_items"][0]["role"] == "user" and "Alpha" in json.dumps(first["input_items"], ensure_ascii=False)
    assert second["previous_response_id"] == "r1"
    outputs = second["input_items"]
    assert [o["call_id"] for o in outputs] == ["c1", "c2"] and all(o["type"] == "function_call_output" for o in outputs)
    assert second["instructions"] == first["instructions"]  # instructions do not persist across turns
    assert second["tools"] == first["tools"]


def test_agent_continues_a_conversation_with_previous_response_id(toolkit):
    model = ScriptedModel([AgentTurn("r9", "Listo.")])
    OperatorAgent(model, toolkit).answer("gracias", previous_response_id="r8")
    assert model.requests[0]["previous_response_id"] == "r8"


def test_agent_stops_a_runaway_tool_loop(toolkit):
    loop = [AgentTurn(f"r{i}", "", (ToolCall(f"c{i}", "rank_sites", {}),)) for i in range(10)]
    reply = OperatorAgent(ScriptedModel(loop), toolkit, max_steps=3).answer("x")
    assert "tool_loop_limit" in reply.flags
    assert reply.answer_es


def test_tool_errors_are_returned_to_the_model_not_raised(toolkit):
    model = ScriptedModel([AgentTurn("r1", "", (ToolCall("c1", "get_site", {"site_id": "nope"}),)), AgentTurn("r2", "No encontré ese sitio.")])
    reply = OperatorAgent(model, toolkit).answer("x")
    assert reply.answer_es == "No encontré ese sitio."
    assert "error" in json.loads(model.requests[1]["input_items"][0]["output"])


def test_agent_answer_that_invents_a_tier_is_replaced(toolkit):
    model = ScriptedModel([AgentTurn("r1", "El relave está en nivel EVACUATION, evacúen ya.")])
    reply = OperatorAgent(model, toolkit).answer("¿cómo está Alpha?")
    assert "tier_claim_removed" in reply.flags
    assert "EVACUATION" not in reply.answer_es


def test_agent_answer_quoting_a_tier_returned_by_a_tool_is_kept(toolkit):
    model = ScriptedModel(
        [
            AgentTurn("r1", "", (ToolCall("c1", "evaluate_alert", {"site_id": "syn-001", "observed_rain_24h_mm": 40, "water_rise_cm": 50}),)),
            AgentTurn("r2", "El nivel es PENDING_CONFIRMATION: el operador debe confirmar."),
        ]
    )
    reply = OperatorAgent(model, toolkit).answer("evalúa Alpha con lluvia 40 mm")
    assert reply.flags == ()
    assert "PENDING_CONFIRMATION" in reply.answer_es


def test_guard_tier_claims_unit():
    text, flags = guard_tier_claims("Nivel ATTENTION para Alpha", allowed={"ATTENTION"})
    assert flags == () and text == "Nivel ATTENTION para Alpha"
    text, flags = guard_tier_claims("Nivel EVACUATION", allowed=set())
    assert flags == ("tier_claim_removed",)
    text, flags = guard_tier_claims("Sin niveles aquí", allowed=set())
    assert flags == ()

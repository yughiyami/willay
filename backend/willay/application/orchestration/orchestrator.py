"""Multi-agent orchestrator on the Responses API.

A planner agent (muse-wonder-1.0, with a cheaper fallback) delegates through tools to four specialist sub-agents
(muse-spark-1.3): HazardAnalyst, Watcher (SAM 3.1 clips), Dispatcher (prepares proposals, never sends) and
Communicator (Spanish messages and Muse Image cards; Quechua only from reviewed templates).

Guardrails enforced in code, not in prompts:
  * Alert tiers come only from the domain rules. The agents cannot pass conditions: `evaluate_alert` reads the run's
    scenario and the clip measurement; an answer that states a tier code no tool returned is replaced.
  * No agent has a tool that sends anything. `prepare_dispatch` only creates a proposal; a human operator approves it
    through the API, and EVACUATION is evaluated with that confirmation at click time.
  * A run has a cost cap and the planner drops to its fallback model when the remaining budget is low or it errors.
  * Trace arguments never contain free text (see trace.summarize_args).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Sequence

from willay.application.compose_alert_message import HOUSEHOLD_LANGUAGE, AlertMessageInput, compose_alert_message
from willay.application.evacuation_card import CardRequest, build_card
from willay.application.evaluate_alert_tier import evaluate_alert_tier
from willay.application.operator_agent import GUARD_FALLBACK_ES, LAYERS, OperatorToolkit, guard_tier_claims
from willay.application.orchestration.runtime import (
    OrchestratorConfig,
    OrchestratorServices,
    Proposal,
)
from willay.application.orchestration.trace import RunTrace, summarize_args, use_agent, use_trace
from willay.application.rank_sites import RankedSite
from willay.application.watch_clip import measure_clip
from willay.domain.entities import AlertTier, HazardContext
from willay.domain.tiers import TierEvaluation
from willay.ports.agent import AgentModelPort, AgentTurn
from willay.ports.templates import TemplateNotAvailable

PLANNER, HAZARD, WATCHER, DISPATCHER, COMMUNICATOR = "planner", "hazard_analyst", "watcher", "dispatcher", "communicator"
RUN_BUDGET_MESSAGE_ES = "Se alcanzó el límite de costo de esta corrida de agentes; no se hicieron más consultas a los modelos."
HOUSEHOLD_TIERS = (AlertTier.PREPARATION, AlertTier.ATTENTION)

__all__ = ["GUARD_FALLBACK_ES", "Orchestrator", "RunBudgetExceeded", "RunResult", "all_tool_names"]


class RunBudgetExceeded(RuntimeError):
    """The estimated cost of this run reached its cap."""


# --- tool definitions ------------------------------------------------------------------------------------------


def _tool(name: str, description: str, properties: dict | None = None, required: Sequence[str] = ()) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False},
    }


_SITE = {"site_id": {"type": "string", "description": "Site id, e.g. ingemmet-1015"}}
_TASK = {"task": {"type": "string", "description": "What you need, in one or two sentences (no personal data)"}}

EVALUATE_ALERT = _tool(
    "evaluate_alert",
    "Alert tier of a site from the deterministic rules, using the conditions of this run. You cannot change the conditions "
    "or confirm an evacuation. The tier is only valid when it comes from here.",
    _SITE,
    ["site_id"],
)

HAZARD_TOOLS = [
    _tool("rank_sites", "Tailings sites ranked by risk (highest first).", {"limit": {"type": "integer"}}),
    _tool("get_site", "One site: location, status, risk, downstream settlements and water intakes.", _SITE, ["site_id"]),
    _tool("get_district", "Static score, CENEPRED baseline, learned score and INDECI emergencies of a district.", {"district": {"type": "string"}}, ["district"]),
    _tool("explain_factors", "Risk factor breakdown of a site.", _SITE, ["site_id"]),
    EVALUATE_ALERT,
    _tool("get_hazard_context", "ENFEN magnitude and communique, seasonal rainfall outlook, SENAMHI level and rain inputs of this run."),
    _tool("get_rain_context", "Recent CHIRPS rainfall accumulation over the basin (satellite estimate, 5 km)."),
    _tool("get_backtest_summary", "District backtest (2017, 2023): AUC with confidence intervals for the static score, CENEPRED, the ensemble and the learned model, with caveats."),
    _tool("get_learned_risk", "Learned district propensity (logistic regression trained on 2003-2016) and its rank.", {"district": {"type": "string"}}, ["district"]),
]
WATCHER_TOOLS = [
    _tool("list_clips", "Ids of the river-watcher clips available in this session."),
    _tool(
        "measure_clip",
        "Measure the water rise (cm) on the painted marker of a clip with SAM 3.1 plus deterministic geometry. A measurement, not an alert.",
        {"clip_id": {"type": "string"}, "marker_height_cm": {"type": "number", "description": "Visible marker length at the start, in cm"}},
        ["clip_id", "marker_height_cm"],
    ),
]
DISPATCHER_TOOLS = [
    EVALUATE_ALERT,
    _tool(
        "prepare_dispatch",
        "Prepare a dispatch PROPOSAL for the households downstream of a site. The tier is decided by the rules, not by you. "
        "Nothing is sent: a human operator must approve the proposal.",
        _SITE,
        ["site_id"],
    ),
    _tool("list_proposals", "Proposals created in this session and their status."),
]
COMMUNICATOR_TOOLS = [
    _tool(
        "compose_message",
        "Household message (four short lines) for the tier the rules currently return. Spanish is the default; other languages "
        "only exist if a human-reviewed template is available (never machine-translated).",
        {**_SITE, "language": {"type": "string", "description": "es (default) or a reviewed language such as qu"}},
        ["site_id"],
    ),
    _tool("generate_card", "One illustrated evacuation card (Muse Image) for the nearest downstream settlement of a site. Needs human review.", _SITE, ["site_id"]),
]
PLANNER_TOOLS = [
    _tool("ask_hazard_analyst", "Delegate hazard analysis: rankings, ENFEN/CHIRPS/SENAMHI context, backtest and learned-model explanations.", _TASK, ["task"]),
    _tool(
        "ask_watcher",
        "Delegate a river-clip analysis (SAM 3.1 water rise on the marker).",
        {**_TASK, "clip_id": {"type": "string"}, "marker_height_cm": {"type": "number"}},
        ["task"],
    ),
    _tool("ask_dispatcher", "Delegate preparing a dispatch proposal for a site (never sends).", {**_TASK, **_SITE}, ["task", "site_id"]),
    _tool("ask_communicator", "Delegate household messages and evacuation cards for a site.", {**_TASK, **_SITE}, ["task", "site_id"]),
    EVALUATE_ALERT,
    _tool("fly_to", "Move the map to a site, district or coordinates.", {**_SITE, "district": {"type": "string"}, "lon": {"type": "number"}, "lat": {"type": "number"}}),
    _tool("show_layer", "Show or hide a map layer.", {"layer": {"type": "string", "enum": list(LAYERS)}, "visible": {"type": "boolean"}}, ["layer", "visible"]),
]

AGENT_TOOLS: Mapping[str, list[dict]] = {
    PLANNER: PLANNER_TOOLS,
    HAZARD: HAZARD_TOOLS,
    WATCHER: WATCHER_TOOLS,
    DISPATCHER: DISPATCHER_TOOLS,
    COMMUNICATOR: COMMUNICATOR_TOOLS,
}


def all_tool_names() -> list[str]:
    return sorted({t["name"] for tools in AGENT_TOOLS.values() for t in tools})


_COMMON_RULES = (
    "Responde SIEMPRE en español, con frases cortas y solo con datos que devuelvan tus herramientas. Los niveles de alerta "
    "(PREPARATION, ATTENTION, PENDING_CONFIRMATION, EVACUATION) los decide únicamente la herramienta evaluate_alert: cítalos tal "
    "como los devuelve y nunca los deduzcas. No puedes enviar alertas ni confirmar evacuaciones: eso lo hace el operador humano. "
    "Esta alerta complementa, y no reemplaza, las alertas oficiales de SENAMHI, INDECI y ENFEN. No incluyas datos personales."
)
INSTRUCTIONS: Mapping[str, str] = {
    PLANNER: (
        "Eres el Planificador de Willay Relaves (alerta temprana por El Niño para relaves mineros en la cuenca del Rímac). "
        "Delegas el trabajo en especialistas mediante herramientas: ask_hazard_analyst (peligro, ranking, contexto ENFEN/CHIRPS/"
        "SENAMHI, backtest), ask_watcher (clip del río con SAM 3.1), ask_dispatcher (prepara propuestas de envío; nunca envía) y "
        "ask_communicator (mensajes en español y fichas con imagen; quechua solo con plantillas revisadas). Llama solo a los "
        "especialistas necesarios y resume el resultado para el operador en máximo 5 frases. " + _COMMON_RULES
    ),
    HAZARD: "Eres el Analista de Peligro de Willay Relaves. Analizas riesgo de relaves y distritos, contexto ENFEN/CHIRPS/SENAMHI y el backtest. "
    "Explica con honestidad las limitaciones. Máximo 4 frases. " + _COMMON_RULES,
    WATCHER: "Eres el Vigía de Willay Relaves. Mides la subida del río en un clip con la herramienta measure_clip (SAM 3.1). "
    "Informa la subida en cm, la confianza y cualquier razón de baja calidad; es una medición, no una alerta. Máximo 3 frases. " + _COMMON_RULES,
    DISPATCHER: "Eres el Despachador de Willay Relaves. Solo preparas propuestas con prepare_dispatch; no existe herramienta de envío. "
    "Indica el nivel que devolvió la herramienta y que el operador debe aprobar. Máximo 3 frases. " + _COMMON_RULES,
    COMMUNICATOR: "Eres el Comunicador de Willay Relaves. Redactas mensajes para hogares con compose_message y fichas con generate_card. "
    "El texto oficial es el que devuelve la herramienta; no lo cambies. El quechua solo existe con plantilla revisada. Máximo 3 frases. " + _COMMON_RULES,
}


# --- run state ---------------------------------------------------------------------------------------------------


@dataclass
class RunState:
    trace: RunTrace
    hazard: HazardContext
    clip_id: str | None = None
    measurements: dict = field(default_factory=dict)
    tiers_returned: set[str] = field(default_factory=set)
    ui_actions: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    artifacts_info: dict = field(default_factory=dict)
    delegations: int = 0

    def context(self) -> HazardContext:
        """The conditions the rules see: the run scenario plus what the watcher measured. Never confirmed."""
        rise = self.measurements.get("water_rise_cm", self.hazard.water_rise_cm)
        return replace(self.hazard, operator_confirmed=False, water_rise_cm=rise)


@dataclass(frozen=True)
class RunResult:
    answer_es: str
    ui_actions: tuple[dict, ...]
    tools_used: tuple[str, ...]
    flags: tuple[str, ...]
    proposals: tuple[dict, ...]
    facts: Mapping
    artifacts_info: Mapping

    def to_dict(self) -> dict:
        return {
            "answer_es": self.answer_es,
            "ui_actions": list(self.ui_actions),
            "tools_used": list(self.tools_used),
            "flags": list(self.flags),
            "proposals": list(self.proposals),
            "facts": dict(self.facts),
            "artifacts_info": dict(self.artifacts_info),
        }


def _scenario_detail(h: HazardContext) -> dict:
    return {
        "enfen_magnitude": h.enfen_magnitude.value,
        "seasonal_rainfall_anomaly": h.seasonal_rainfall_anomaly.value,
        "senamhi_level": h.senamhi_level.value,
        "forecast_rain_72h_mm": h.forecast_rain_72h_mm,
        "observed_rain_24h_mm": h.observed_rain_24h_mm,
        "water_rise_cm": h.water_rise_cm,
    }


def _result_summary(name: str, result: Mapping) -> str:
    if "error" in result:
        return f"error: {str(result['error'])[:140]}"
    if name == "evaluate_alert":
        return f"tier={result.get('tier')} site={result.get('site_id')}"
    if name == "measure_clip":
        return f"rise_cm={result.get('rise_cm')} confidence={result.get('confidence')} valid_frames={result.get('valid_frames')}/{result.get('total_frames')}"
    if name == "prepare_dispatch":
        proposal = result.get("proposal")
        return "no proposal: " + str(result.get("reason")) if proposal is None else f"proposal {proposal['id']} tier={proposal['tier']} status={proposal['status']}"
    if name == "compose_message":
        return f"available={result.get('available')} tier={result.get('tier')} language={result.get('language')}"
    if name == "generate_card":
        return f"card review_required={result.get('review_required')}"
    if name.startswith("ask_"):
        return f"{result.get('agent')} answered {len(str(result.get('answer_es', '')))} chars"
    if name == "rank_sites":
        return f"{len(result.get('sites', []))} sites"
    keys = ", ".join(list(result)[:6])
    return f"keys: {keys}"


class Orchestrator:
    def __init__(self, model: AgentModelPort, services: OrchestratorServices, config: OrchestratorConfig | None = None) -> None:
        self._model = model
        self._s = services
        self._cfg = config or OrchestratorConfig()
        # Tiers a tool returned earlier in this session (for example in the previous stage of the demo) may be cited later.
        self._session_tiers: set[str] = set()

    # --- entry point -------------------------------------------------------------------------------------------

    def run(self, text: str, trace: RunTrace, scenario: HazardContext | None = None, clip_id: str | None = None) -> RunResult:
        state = RunState(trace=trace, hazard=replace(scenario or self._s.hazard, operator_confirmed=False), clip_id=clip_id)
        state.tiers_returned |= self._session_tiers
        with use_trace(trace):
            trace.emit(
                "run_start",
                agent=PLANNER,
                args_summary=summarize_args({"text": text, "clip_id": clip_id or ""}),
                detail={
                    "scenario": _scenario_detail(state.hazard),
                    "planner_model": self._cfg.planner_model,
                    "specialist_model": self._cfg.specialist_model,
                },
            )
            try:
                answer = self._planner(state, text)
            except RunBudgetExceeded:
                state.flags.append("run_budget_exceeded")
                trace.emit("guardrail", agent=PLANNER, result_summary="run_budget_exceeded", detail={"cap_usd": self._cfg.run_budget_usd})
                answer = RUN_BUDGET_MESSAGE_ES
        answer, guard_flags = guard_tier_claims(answer, set(state.tiers_returned))
        if guard_flags:
            state.flags.extend(guard_flags)
            trace.emit("guardrail", agent=PLANNER, result_summary="tier_claim_removed: a tier code no tool returned was in the answer")
        self._session_tiers |= state.tiers_returned
        return RunResult(
            answer_es=answer,
            ui_actions=tuple(state.ui_actions),
            tools_used=tuple(dict.fromkeys(state.tools_used)),
            flags=tuple(dict.fromkeys(state.flags)),
            proposals=tuple(p.to_dict() for p in state.proposals),
            facts=dict(state.measurements),
            artifacts_info=dict(state.artifacts_info),
        )

    # --- agent loops -------------------------------------------------------------------------------------------

    def _check_budget(self, state: RunState) -> None:
        if state.trace.total_cost() >= self._cfg.run_budget_usd:
            raise RunBudgetExceeded()

    def _user_item(self, text: str) -> dict:
        return {"role": "user", "content": [{"type": "input_text", "text": text}]}

    def _planner(self, state: RunState, text: str) -> str:
        remaining = self._s.budget_remaining()
        model = self._cfg.planner_model
        if remaining is not None and remaining < self._cfg.min_planner_budget_usd:
            model = self._cfg.planner_fallback_model
            state.flags.append("planner_fallback")
            state.trace.emit("guardrail", agent=PLANNER, result_summary=f"planner_fallback: remaining budget ${remaining:.2f} is below ${self._cfg.min_planner_budget_usd:.2f}", detail={"model": model})
        return self._loop(state, PLANNER, text, model, self._cfg.planner_max_output_tokens, fallback=True)

    def _loop(self, state: RunState, agent: str, task: str, model: str, max_output: int, fallback: bool = False) -> str:
        tools = AGENT_TOOLS[agent]
        gathered: list[dict] = []
        with use_agent(agent):
            state.trace.emit("agent_start", model=model, args_summary=summarize_args({"task": task}))
            items: list[Mapping] = [self._user_item(task)]
            previous: str | None = None
            turn: AgentTurn | None = None
            steps = 0
            while True:
                self._check_budget(state)
                try:
                    turn = self._model.run_turn(
                        instructions=INSTRUCTIONS[agent], input_items=items, tools=tools,
                        previous_response_id=previous, purpose=agent, model=model, max_output_tokens=max_output,
                    )
                except RunBudgetExceeded:
                    raise
                except Exception as exc:
                    if not fallback or model == self._cfg.planner_fallback_model:
                        raise
                    model = self._cfg.planner_fallback_model
                    state.flags.append("planner_fallback")
                    state.trace.emit("guardrail", result_summary=f"planner_fallback: {type(exc).__name__} from the planner model", detail={"model": model})
                    recap = json.dumps(gathered, ensure_ascii=False, default=str)[:6000]
                    items = [self._user_item(task + ("\n\nResultados de herramientas hasta ahora (JSON): " + recap if gathered else ""))]
                    previous = None
                    self._check_budget(state)
                    turn = self._model.run_turn(
                        instructions=INSTRUCTIONS[agent], input_items=items, tools=tools,
                        previous_response_id=None, purpose=agent, model=model, max_output_tokens=max_output,
                    )
                if not turn.tool_calls:
                    break
                if steps >= self._cfg.max_agent_steps:
                    state.flags.append(f"{agent}_tool_loop_limit")
                    break
                steps += 1
                outputs = []
                for call in turn.tool_calls:
                    result = self._run_tool(state, agent, call.name, call.arguments)
                    gathered.append({"tool": call.name, "result": result})
                    outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, ensure_ascii=False, default=str)})
                items, previous = outputs, turn.response_id
            text = (turn.text or "").strip() if turn else ""
            if not text:
                done = ", ".join(dict.fromkeys(g["tool"] for g in gathered))
                text = (
                    f"Herramientas ejecutadas: {done}. El modelo no devolvió un comentario final; revise los resultados en el registro."
                    if gathered
                    else "No pude completar la consulta con las herramientas disponibles."
                )
            state.trace.emit("agent_end", result_summary=f"{len(text)} chars", detail={"text": text[:1500]})
        return text

    def _run_tool(self, state: RunState, agent: str, name: str, args: Mapping) -> dict:
        if name not in {t["name"] for t in AGENT_TOOLS[agent]}:
            state.trace.emit("tool_call", agent=agent, tool=name, status="error", result_summary="tool not available to this agent")
            return {"error": f"unknown tool '{name}'"}
        started = time.perf_counter()
        status = "ok"
        try:
            result, actions = getattr(self, f"_tool_{name}")(state, agent, args)
            state.ui_actions.extend(actions)
        except RunBudgetExceeded:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            result, status = {"error": f"invalid arguments: {exc}"}, "error"
        except Exception as exc:  # a failing tool or sub-agent must not end the whole run
            result, status = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}, "error"
            state.flags.append(f"{name}_failed")
        if "error" in result:
            status = "error"
        state.tools_used.append(f"{agent}.{name}")
        state.trace.emit(
            "tool_call", agent=agent, tool=name, status=status, latency_ms=int((time.perf_counter() - started) * 1000),
            args_summary=summarize_args(args, safe_text_keys=("language", "layer")), result_summary=_result_summary(name, result),
        )
        return result

    # --- shared helpers ------------------------------------------------------------------------------------------

    def _find(self, site_id: object) -> RankedSite | None:
        return next((r for r in self._s.ranked() if r.site.id == site_id), None)

    def _evaluate(self, state: RunState, ranked: RankedSite) -> tuple[TierEvaluation, HazardContext]:
        context = state.context()
        evaluation = evaluate_alert_tier(context, ranked.static_risk, self._s.tier_config)
        state.tiers_returned.add(evaluation.tier.value)
        return evaluation, context

    def _operator_toolkit(self, state: RunState) -> OperatorToolkit:
        return OperatorToolkit(self._s.ranked, state.context(), districts=self._s.districts)

    # --- planner tools: delegation ---------------------------------------------------------------------------------

    def _delegate(self, state: RunState, agent: str, task: str, extra: str = "") -> dict:
        state.delegations += 1
        if state.delegations > self._cfg.max_delegations:
            state.flags.append("delegation_limit")
            return {"error": "delegation limit reached for this run"}
        answer = self._loop(state, agent, task + extra, self._cfg.specialist_model, self._cfg.specialist_max_output_tokens)
        answer, flags = guard_tier_claims(answer, set(state.tiers_returned))
        if flags:
            state.flags.extend(flags)
            state.trace.emit("guardrail", agent=agent, result_summary="tier_claim_removed: specialist answer replaced")
        return {"agent": agent, "answer_es": answer, "facts": dict(state.measurements), "tiers_returned": sorted(state.tiers_returned)}

    def _tool_ask_hazard_analyst(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._delegate(state, HAZARD, str(args.get("task", ""))), []

    def _tool_ask_watcher(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        clip_id = args.get("clip_id") or state.clip_id or (self._s.clips.ids() or [None])[0]
        marker = args.get("marker_height_cm")
        extra = f"\nclip_id: {clip_id}" if clip_id else "\n(no hay clips disponibles)"
        if marker:
            extra += f"\nmarker_height_cm: {float(marker)}"
        return self._delegate(state, WATCHER, str(args.get("task", "")), extra), []

    def _tool_ask_dispatcher(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._delegate(state, DISPATCHER, str(args.get("task", "")), f"\nsite_id: {args.get('site_id')}"), []

    def _tool_ask_communicator(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._delegate(state, COMMUNICATOR, str(args.get("task", "")), f"\nsite_id: {args.get('site_id')}"), []

    def _tool_fly_to(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._operator_toolkit(state).call("fly_to", args)

    def _tool_show_layer(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._operator_toolkit(state).call("show_layer", args)

    # --- hazard analyst tools -----------------------------------------------------------------------------------------

    def _tool_rank_sites(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._operator_toolkit(state).call("rank_sites", args)

    def _tool_get_site(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._operator_toolkit(state).call("get_site", args)

    def _tool_explain_factors(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return self._operator_toolkit(state).call("explain_factors", args)

    def _tool_get_district(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        result, actions = self._operator_toolkit(state).call("get_district", args)
        feature = self._operator_toolkit(state)._find_district(args.get("district"))  # noqa: SLF001
        learned = (feature or {}).get("properties", {}).get("learned") if feature else None
        if "error" not in result:
            result["learned"] = learned
        return result, actions

    def _tool_evaluate_alert(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        ranked = self._find(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        evaluation, context = self._evaluate(state, ranked)
        return {
            "site_id": ranked.site.id,
            "tier": evaluation.tier.value,
            "triggered": [t.value for t in evaluation.triggered],
            "reasons": list(evaluation.reasons),
            "requires_operator_confirmation": evaluation.tier is AlertTier.PENDING_CONFIRMATION,
            "inputs": _scenario_detail(context) | {"watcher_report": context.watcher_report},
            "note": "conditions come from the run scenario and the clip measurement; they cannot be changed by an agent",
        }, []

    def _tool_get_hazard_context(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return {
            **_scenario_detail(state.context()),
            "watcher_report": state.context().watcher_report,
            "official_source": self._s.official_source,
            "note": "ENFEN comes from the curated communique timeline; SENAMHI level and rain inputs are scenario inputs set by the "
            "operator (a live SENAMHI feed is not connected).",
        }, []

    def _tool_get_rain_context(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        if not self._s.rain_context:
            return {"available": False, "note": "no CHIRPS series loaded in this data mode"}, []
        return {"available": True, **self._s.rain_context}, []

    def _tool_get_backtest_summary(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        events = (self._s.backtest_results or {}).get("events")
        if not events:
            return {"available": False, "note": "no stored backtest in this data mode"}, []
        summary: dict = {}
        for key, event in events.items():
            summary[key] = {
                "n_districts": event.get("n_districts"),
                "n_positive": event.get("n_positive"),
                "auc": {
                    name: {"auc": m.get("auc"), "ci95": m.get("auc_ci95")}
                    for name, m in event.get("models", {}).items()
                    if name in ("willay_static", "cenepred_baseline", "ensemble_static_cenepred", "population_baseline")
                },
            }
        learned = (self._s.learned_model or {}).get("evaluation")
        return {
            "available": True,
            "events": summary,
            "learned_model": None if not learned else {k: v.get("learned") for k, v in learned.items()},
            "caveats": [
                "only two events and 78 districts: differences below about 0.1 AUC are not distinguishable",
                "the learned model (train 2003-2016) did not beat the a-priori static score; its propensity is for a typical season",
                "the PREPARATION rule as written would not have fired in 2017 or 2023",
            ],
        }, []

    def _tool_get_learned_risk(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        toolkit = self._operator_toolkit(state)
        feature = toolkit._find_district(args.get("district"))  # noqa: SLF001
        if feature is None:
            return {"error": f"unknown district '{args.get('district')}'"}, []
        learned = feature["properties"].get("learned")
        if not learned:
            return {"available": False, "note": "no learned model for this data mode"}, []
        return {"district": feature["properties"].get("name"), **learned, "note": "relative propensity for a typical Jan-Apr season; not calibrated for El Niño seasons"}, []

    # --- watcher tools ----------------------------------------------------------------------------------------------------

    def _tool_list_clips(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return {"clips": self._s.clips.ids()}, []

    def _tool_measure_clip(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        clip = self._s.clips.get(str(args.get("clip_id")))
        if clip is None:
            return {"error": f"unknown clip '{args.get('clip_id')}'"}, []
        if self._s.segmenter is None:
            return {"error": "the segmentation model is not configured"}, []
        marker = float(args.get("marker_height_cm") or 0)
        if marker <= 0:
            return {"error": "marker_height_cm must be positive"}, []
        suffix = ".mov" if clip.content_type == "video/quicktime" else ".mp4"
        total = self._s.clip_probe(clip.data, suffix) if self._s.clip_probe else None
        measurement = measure_clip(
            clip.data, self._s.segmenter, marker, water_prompt=self._s.water_prompt, marker_prompt=self._s.marker_prompt, total_frames=total
        )
        watch = measurement.watch
        rise = None if watch.rise_cm is None else round(watch.rise_cm, 1)
        if rise is not None:
            state.measurements["water_rise_cm"] = rise
        info = {
            "rise_cm": rise,
            "confidence": watch.confidence,
            "valid_frames": watch.valid_frames,
            "total_frames": watch.total_frames,
            "reasons": list(watch.reasons),
            "billed_frames": measurement.billed_frames,
            "marker_height_cm": marker,
            "note": "a measurement, not an alert: the tier comes from the rules",
        }
        state.artifacts_info["watch"] = dict(info)
        if self._s.overlay_renderer is not None and rise is not None:
            names = []
            for index, png in self._s.overlay_renderer(clip.data, suffix, measurement):
                name = f"overlay_{index}.png"
                state.trace.add_artifact(name, "image/png", png)
                names.append(name)
            state.artifacts_info["watch"]["overlays"] = names
        return info, []

    # --- dispatcher tools --------------------------------------------------------------------------------------------------

    def _tool_prepare_dispatch(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        ranked = self._find(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        evaluation, context = self._evaluate(state, ranked)
        if evaluation.tier in HOUSEHOLD_TIERS:
            tier = evaluation.tier
        elif evaluation.tier is AlertTier.PENDING_CONFIRMATION:
            tier = AlertTier.EVACUATION
        else:
            return {"proposal": None, "reason": f"the rules return {evaluation.tier.value}: no household alert applies"}, []
        for existing in state.proposals:
            if existing.site_id == ranked.site.id and existing.tier is tier and existing.status == "awaiting_operator":
                return {"proposal": existing.to_dict(), "note": "an identical proposal already awaits the operator"}, []
        proposal = self._s.proposals.add(
            Proposal(
                run_id=state.trace.run_id,
                site_id=ranked.site.id,
                site_name=ranked.site.name,
                tier=tier,
                evaluated_tier=evaluation.tier.value,
                reasons=evaluation.reasons,
                context=context,
                households_targeted=self._s.households(ranked.site.id),
                tier_config=self._s.tier_config,
            )
        )
        state.proposals.append(proposal)
        state.trace.emit(
            "proposal",
            result_summary=f"proposal {proposal.id}: {tier.value} for {ranked.site.name}, awaiting operator",
            detail={"proposal_id": proposal.id, "tier": tier.value, "evaluated_tier": evaluation.tier.value, "households": proposal.households_targeted},
        )
        return {
            "proposal": proposal.to_dict(),
            "note": "nothing was sent; a human operator must approve this proposal with a confirmation id",
        }, []

    def _tool_list_proposals(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        return {"proposals": [p.to_dict() for p in self._s.proposals.list()]}, []

    # --- communicator tools -----------------------------------------------------------------------------------------------------

    def _message_tier(self, state: RunState, ranked: RankedSite) -> tuple[AlertTier | None, bool]:
        evaluation, _ = self._evaluate(state, ranked)
        if evaluation.tier in HOUSEHOLD_TIERS:
            return evaluation.tier, False
        if evaluation.tier is AlertTier.PENDING_CONFIRMATION:
            return AlertTier.EVACUATION, True  # a draft: it is not valid until the operator confirms
        return None, False

    def _nearest(self, ranked: RankedSite):
        nearest = ranked.exposure.nearest if ranked.exposure else None
        if nearest is None:
            return None, None
        settlement = next((s for s in self._s.settlements() if s.id == nearest.settlement_id), None)
        return nearest, settlement

    def _tool_compose_message(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        ranked = self._find(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        tier, draft = self._message_tier(state, ranked)
        if tier is None:
            return {"available": False, "reason": "no household alert applies under the current rules"}, []
        nearest, settlement = self._nearest(ranked)
        if nearest is None:
            return {"available": False, "reason": "this site has no downstream settlement to alert"}, []
        language = str(args.get("language") or HOUSEHOLD_LANGUAGE)
        try:
            message = compose_alert_message(
                AlertMessageInput(
                    tier=tier, site_name=ranked.site.name, distance_km=nearest.along_path_m / 1000.0,
                    safe_point=settlement.safe_point if settlement else None, official_source=self._s.official_source,
                    proposed_safe_point=settlement.proposed_safe_point if settlement else None,
                ),
                llm=self._s.llm, language=language, templates=self._s.templates,
            )
        except TemplateNotAvailable as unavailable:
            return {"available": False, "language": unavailable.language, "reason": str(unavailable)}, []
        state.tiers_returned.add(message.tier.value)  # a tool returned this tier (a draft when pending confirmation)
        info = {"available": True, "tier": message.tier.value, "language": message.language, "text": message.text, "draft_pending_confirmation": draft, "rephrased": message.rephrased}
        state.artifacts_info["message"] = info
        return info, []

    def _tool_generate_card(self, state: RunState, agent: str, args: Mapping) -> tuple[dict, list]:
        if self._s.image is None:
            return {"error": "the image model is not configured"}, []
        ranked = self._find(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        tier, draft = self._message_tier(state, ranked)
        if tier is None:
            return {"error": "no household alert applies under the current rules"}, []
        nearest, settlement = self._nearest(ranked)
        if nearest is None:
            return {"error": "this site has no downstream settlement"}, []
        card = build_card(
            CardRequest(
                nearest.settlement_name, nearest.along_path_m / 1000.0, settlement.safe_point if settlement else None, tier,
                settlement.proposed_safe_point if settlement else None,
            ),
            self._s.image,
        )
        state.tiers_returned.add(tier.value)
        state.trace.add_artifact("card.png", "image/png", card.image)
        info = {"review_required": card.review_required, "intended_text": card.intended_text, "artifact": "card.png", "tier": tier.value, "draft_pending_confirmation": draft}
        state.artifacts_info["card"] = info
        return info, []

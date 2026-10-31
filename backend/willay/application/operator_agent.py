"""Use case: the operator's voice/text agent.

A tool-calling language model answers in Spanish and drives the map through `ui_actions`. Every fact comes
from a tool that calls our use cases; alert tiers come only from `evaluate_alert_tier` (domain rules). The
agent cannot confirm an evacuation (the tool has no such input), and an answer that states a tier code no
tool returned in this turn is replaced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Sequence

from willay.application.evaluate_alert_tier import evaluate_alert_tier
from willay.application.rank_sites import RankedSite
from willay.domain.entities import AlertTier, HazardContext, SenamhiLevel
from willay.ports.agent import AgentModelPort

LAYERS = ("sites", "flowpaths", "settlements", "districts", "intakes", "pasivos")
SITE_HEIGHT_M = 8_000.0
DISTRICT_HEIGHT_M = 40_000.0
POINT_HEIGHT_M = 20_000.0

SYSTEM_PROMPT_ES = (
    "Eres el asistente del operador de Willay Relaves, un sistema de alerta temprana por el Fenómeno El Niño para "
    "relaves mineros y comunidades río abajo en la cuenca del Rímac (Perú). Responde SIEMPRE en español, en tono "
    "claro y profesional, con frases cortas (máximo 4). Usa únicamente datos que devuelvan las herramientas; no "
    "inventes cifras. Los niveles de alerta (PREPARATION, ATTENTION, PENDING_CONFIRMATION, EVACUATION) solo los "
    "determina la herramienta evaluate_alert: cita el nivel tal como lo devuelve y nunca lo subas, bajes ni "
    "deduzcas por tu cuenta. Tú no puedes confirmar una evacuación: eso lo hace el operador humano. Para mover "
    "el mapa usa fly_to y show_layer. Esta alerta complementa, y no reemplaza, las alertas oficiales de SENAMHI, "
    "INDECI y ENFEN."
)

_TIER_CODES = tuple(t.value for t in AlertTier if t is not AlertTier.NONE)
_TIER_PATTERN = re.compile(r"\b(" + "|".join(_TIER_CODES) + r")\b")
GUARD_FALLBACK_ES = (
    "No puedo confirmar un nivel de alerta por mi cuenta. Use la evaluación de alerta del sistema (reglas oficiales) "
    "para conocer el nivel de este sitio."
)


def _tool(name: str, description: str, properties: dict, required: Sequence[str] = ()) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
    }


TOOL_DEFINITIONS: list[dict] = [
    _tool(
        "rank_sites",
        "List tailings sites ranked by risk score (highest first) with district, risk and static risk.",
        {"limit": {"type": "integer", "description": "How many sites to return (default 5, max 20)"}},
    ),
    _tool(
        "get_site",
        "Detail of one tailings site: location, status, risk, downstream settlements reached and water intakes.",
        {"site_id": {"type": "string", "description": "Site id, e.g. ingemmet-123"}},
        ["site_id"],
    ),
    _tool(
        "get_district",
        "Static risk score, CENEPRED baseline and recorded emergencies of a district.",
        {"district": {"type": "string", "description": "District name or 6-digit ubigeo"}},
        ["district"],
    ),
    _tool(
        "evaluate_alert",
        "Evaluate the alert tier of a site with the deterministic rules. The tier comes only from here. You may pass "
        "observed or forecast conditions; operator confirmation cannot be passed.",
        {
            "site_id": {"type": "string"},
            "observed_rain_24h_mm": {"type": "number"},
            "forecast_rain_72h_mm": {"type": "number"},
            "water_rise_cm": {"type": "number"},
            "senamhi_level": {"type": "string", "enum": [s.value for s in SenamhiLevel]},
            "watcher_report": {"type": "boolean", "description": "A community watcher reported the river rising"},
        },
        ["site_id"],
    ),
    _tool(
        "show_layer",
        "Show or hide a map layer.",
        {"layer": {"type": "string", "enum": list(LAYERS)}, "visible": {"type": "boolean"}},
        ["layer", "visible"],
    ),
    _tool(
        "fly_to",
        "Move the map camera to a site (also highlights it and its flow path), a district, or coordinates.",
        {
            "site_id": {"type": "string"},
            "district": {"type": "string"},
            "lon": {"type": "number"},
            "lat": {"type": "number"},
        },
    ),
    _tool(
        "explain_factors",
        "The risk factor breakdown of a site (trigger, susceptibility, tailings vulnerability, downstream exposure), "
        "to explain why it ranks where it does.",
        {"site_id": {"type": "string"}},
        ["site_id"],
    ),
]


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _geometry_center(geometry: Mapping | None) -> tuple[float, float] | None:
    if not geometry:
        return None
    points: list[tuple[float, float]] = []

    def walk(node) -> None:
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                points.append((float(node[0]), float(node[1])))
            else:
                for child in node:
                    walk(child)

    walk(geometry.get("coordinates"))
    if not points:
        return None
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    return ((min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0)


class OperatorToolkit:
    """Executes the agent's tools. Each call returns (result for the model, ui_actions for the web map)."""

    def __init__(
        self,
        ranked_provider: Callable[[], Sequence[RankedSite]],
        hazard: HazardContext,
        districts: Mapping | None = None,
    ) -> None:
        self._ranked = ranked_provider
        self._hazard = hazard
        self._districts = districts
        self.tiers_returned: set[str] = set()

    # --- helpers -----------------------------------------------------------------------------

    def _find_site(self, site_id: object) -> RankedSite | None:
        return next((r for r in self._ranked() if r.site.id == site_id), None)

    def _find_district(self, query: object) -> Mapping | None:
        if not self._districts or not isinstance(query, str):
            return None
        wanted = _normalize(query)
        for feature in self._districts.get("features", []):
            props = feature.get("properties", {})
            if wanted in (_normalize(str(props.get("name", ""))), str(props.get("ubigeo", ""))):
                return feature
        return None

    # --- dispatch ------------------------------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, object]) -> tuple[dict, list[dict]]:
        handler = getattr(self, f"_tool_{name}", None) if name in {t["name"] for t in TOOL_DEFINITIONS} else None
        if handler is None:
            return {"error": f"unknown tool '{name}'"}, []
        try:
            return handler(arguments)
        except (TypeError, ValueError) as exc:
            return {"error": f"invalid arguments: {exc}"}, []

    # --- tools ----------------------------------------------------------------------------------

    def _tool_rank_sites(self, args: Mapping) -> tuple[dict, list[dict]]:
        limit = max(1, min(int(args.get("limit", 5) or 5), 20))
        sites = [
            {
                "id": r.site.id,
                "name": r.site.name,
                "district": r.site.district,
                "rank": r.rank,
                "risk": r.score.value,
                "static_risk": r.static_risk,
            }
            for r in list(self._ranked())[:limit]
        ]
        return {"sites": sites}, []

    def _tool_get_site(self, args: Mapping) -> tuple[dict, list[dict]]:
        ranked = self._find_site(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        exposure = ranked.exposure
        return {
            "id": ranked.site.id,
            "name": ranked.site.name,
            "district": ranked.site.district,
            "province": ranked.site.province,
            "lon": ranked.site.lon,
            "lat": ranked.site.lat,
            "status": ranked.site.status.value,
            "declared_risk": ranked.site.declared_risk.value,
            "volume_m3": ranked.site.volume_m3,
            "rank": ranked.rank,
            "risk": ranked.score.value,
            "static_risk": ranked.static_risk,
            "data_quality": list(ranked.site.data_quality),
            "settlements_reached": [] if exposure is None else [c.to_dict() for c in exposure.contributions[:8]],
            "water_intakes": [] if exposure is None else [i.to_dict() for i in exposure.intakes[:8]],
        }, []

    def _tool_get_district(self, args: Mapping) -> tuple[dict, list[dict]]:
        if not self._districts:
            return {"error": "district data is not available in this data mode"}, []
        feature = self._find_district(args.get("district"))
        if feature is None:
            return {"error": f"unknown district '{args.get('district')}'"}, []
        props = feature["properties"]
        return {
            "ubigeo": props.get("ubigeo"),
            "name": props.get("name"),
            "static_score": props.get("static_score"),
            "cenepred_baseline": props.get("cenepred"),
            "emergencies": props.get("emergencies"),
        }, []

    def _tool_evaluate_alert(self, args: Mapping) -> tuple[dict, list[dict]]:
        ranked = self._find_site(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        changes: dict[str, object] = {}
        for key in ("observed_rain_24h_mm", "forecast_rain_72h_mm", "water_rise_cm"):
            if args.get(key) is not None:
                changes[key] = float(args[key])  # type: ignore[arg-type]
        if args.get("senamhi_level") is not None:
            changes["senamhi_level"] = SenamhiLevel(str(args["senamhi_level"]))
        if args.get("watcher_report") is not None:
            changes["watcher_report"] = bool(args["watcher_report"])
        # operator_confirmed is never taken from the agent: only the human operator confirms an evacuation.
        context = replace(self._hazard, operator_confirmed=False, **changes)
        evaluation = evaluate_alert_tier(context, ranked.static_risk)
        self.tiers_returned.add(evaluation.tier.value)
        return {
            "site_id": ranked.site.id,
            "tier": evaluation.tier.value,
            "triggered": [t.value for t in evaluation.triggered],
            "reasons": list(evaluation.reasons),
            "requires_operator_confirmation": evaluation.tier is AlertTier.PENDING_CONFIRMATION,
            "inputs": {
                "enfen_magnitude": context.enfen_magnitude.value,
                "seasonal_rainfall_anomaly": context.seasonal_rainfall_anomaly.value,
                "senamhi_level": context.senamhi_level.value,
                "forecast_rain_72h_mm": context.forecast_rain_72h_mm,
                "observed_rain_24h_mm": context.observed_rain_24h_mm,
                "water_rise_cm": context.water_rise_cm,
                "watcher_report": context.watcher_report,
            },
        }, []

    def _tool_show_layer(self, args: Mapping) -> tuple[dict, list[dict]]:
        layer = args.get("layer")
        if layer not in LAYERS:
            return {"error": f"unknown layer '{layer}' (use one of {', '.join(LAYERS)})"}, []
        visible = bool(args.get("visible", True))
        return {"ok": True, "layer": layer, "visible": visible}, [{"type": "toggle_layer", "layer": layer, "visible": visible}]

    def _tool_fly_to(self, args: Mapping) -> tuple[dict, list[dict]]:
        if args.get("site_id") is not None:
            ranked = self._find_site(args["site_id"])
            if ranked is None:
                return {"error": f"unknown site '{args['site_id']}'"}, []
            site = ranked.site
            return {"ok": True, "target": site.name}, [
                {"type": "fly_to", "lon": site.lon, "lat": site.lat, "height_m": SITE_HEIGHT_M, "label": site.name},
                {"type": "highlight_site", "site_id": site.id},
                {"type": "highlight_flow_path", "site_id": site.id},
            ]
        if args.get("district") is not None:
            feature = self._find_district(args["district"])
            center = _geometry_center(feature.get("geometry")) if feature else None
            if feature is None or center is None:
                return {"error": f"unknown district '{args['district']}' or it has no geometry"}, []
            name = feature["properties"].get("name")
            return {"ok": True, "target": name}, [
                {"type": "fly_to", "lon": center[0], "lat": center[1], "height_m": DISTRICT_HEIGHT_M, "label": name}
            ]
        lon, lat = args.get("lon"), args.get("lat")
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)) and -180 <= lon <= 180 and -90 <= lat <= 90:
            return {"ok": True, "target": f"{lon:.4f}, {lat:.4f}"}, [
                {"type": "fly_to", "lon": float(lon), "lat": float(lat), "height_m": POINT_HEIGHT_M, "label": None}
            ]
        return {"error": "provide site_id, district, or valid lon and lat"}, []

    def _tool_explain_factors(self, args: Mapping) -> tuple[dict, list[dict]]:
        ranked = self._find_site(args.get("site_id"))
        if ranked is None:
            return {"error": f"unknown site '{args.get('site_id')}'"}, []
        return {
            "site_id": ranked.site.id,
            "name": ranked.site.name,
            "risk": ranked.score.value,
            "static_risk": ranked.static_risk,
            "factors": {name: f.to_dict() for name, f in ranked.score.factors.items()},
            "missing_factors": list(ranked.score.missing_factors),
            "data_quality": list(ranked.site.data_quality),
            "formula": "risk = trigger x susceptibility x tailings_vulnerability x downstream_exposure (each in [0, 1])",
        }, []


def guard_tier_claims(answer: str, allowed: set[str]) -> tuple[str, tuple[str, ...]]:
    """Replace an answer that states an alert-tier code no tool returned in this turn."""
    claimed = set(_TIER_PATTERN.findall(answer))
    if claimed - allowed:
        return GUARD_FALLBACK_ES, ("tier_claim_removed",)
    return answer, ()


@dataclass(frozen=True)
class AgentReply:
    answer_es: str
    ui_actions: tuple[dict, ...]
    tool_calls: tuple[dict, ...]
    flags: tuple[str, ...]
    response_id: str


class OperatorAgent:
    def __init__(self, model: AgentModelPort, toolkit: OperatorToolkit, max_steps: int = 5) -> None:
        self._model = model
        self._toolkit = toolkit
        self._max_steps = max_steps

    def answer(self, text: str, previous_response_id: str | None = None) -> AgentReply:
        flags: list[str] = []
        ui_actions: list[dict] = []
        trace: list[dict] = []
        turn = self._model.run_turn(
            instructions=SYSTEM_PROMPT_ES,
            input_items=[{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            tools=TOOL_DEFINITIONS,
            previous_response_id=previous_response_id,
            purpose="agent",
        )
        steps = 0
        while turn.tool_calls:
            if steps >= self._max_steps:
                flags.append("tool_loop_limit")
                break
            steps += 1
            outputs = []
            for call in turn.tool_calls:
                result, actions = self._toolkit.call(call.name, call.arguments)
                ui_actions.extend(actions)
                trace.append({"name": call.name, "arguments": dict(call.arguments)})
                outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, ensure_ascii=False, default=str)})
            turn = self._model.run_turn(
                instructions=SYSTEM_PROMPT_ES,
                input_items=outputs,
                tools=TOOL_DEFINITIONS,
                previous_response_id=turn.response_id,
                purpose="agent",
            )
        answer = turn.text.strip()
        if "tool_loop_limit" in flags or not answer:
            answer = answer or "No pude completar la consulta con las herramientas disponibles. Intente reformular la pregunta."
        answer, guard_flags = guard_tier_claims(answer, set(self._toolkit.tiers_returned))
        return AgentReply(answer, tuple(ui_actions), tuple(trace), tuple(flags) + guard_flags, turn.response_id)

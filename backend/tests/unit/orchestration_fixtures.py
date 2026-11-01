"""Shared fakes for the orchestrator tests: scripted models per model name, synthetic services, a tiny rising-water clip."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from willay.adapters.clip_media import ColorSegmenter
from willay.adapters.synthetic_clip import SyntheticClipSpec, render_clip, write_mp4
from willay.adapters.traced_models import TracedAgentModel
from willay.api.dependencies import synthetic_dependencies
from willay.application.orchestration.orchestrator import Orchestrator
from willay.application.orchestration.runtime import ClipStore, OrchestratorConfig, OrchestratorServices, ProposalStore
from willay.application.orchestration.trace import TraceStore, use_trace
from willay.application.rank_sites import rank_sites
from willay.domain.entities import AlertTier, EnfenMagnitude, HazardContext, SenamhiLevel
from willay.ports.agent import AgentTurn, ToolCall

WONDER = "muse-wonder-1.0"
SPARK = "muse-spark-1.3"


class ScriptedByModel:
    """Returns the next scripted turn for the requested model; an Exception in the script is raised instead."""

    def __init__(self, scripts: dict[str, list]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.requests: list[dict] = []

    def run_turn(self, **kwargs):
        self.requests.append(kwargs)
        item = self.scripts[kwargs.get("model") or SPARK].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(f"call-{name}", name, arguments)


def turn(text: str = "", *calls: ToolCall, model: str = SPARK, rid: str = "r") -> AgentTurn:
    return AgentTurn(rid, text, tuple(calls), model=model, input_tokens=500, output_tokens=100)


def rising_clip_bytes(rise_cm: float = 45.0) -> bytes:
    spec = SyntheticClipSpec(seconds=3.0, fps=10, marker_height_cm=100.0, rise_cm=rise_cm)
    frames, _ = render_clip(spec)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "clip.mp4"
        write_mp4(frames, spec.fps, path)
        return path.read_bytes()


def build_services(**overrides) -> OrchestratorServices:
    deps = synthetic_dependencies()
    ranked = rank_sites(
        deps.repository, deps.hazard_scenario, exposure_config=deps.exposure_config,
        intakes=list(deps.repository.list_water_intakes()), basin_only=False,
    )
    values = dict(
        ranked=lambda: ranked,
        hazard=deps.hazard_scenario,
        official_source=deps.official_source,
        settlements=lambda: list(deps.repository.list_settlements()),
        households=lambda site_id: sum(
            1 for r in ranked if r.site.id == site_id and r.exposure
            for h in deps.households if h.settlement_id in {c.settlement_id for c in r.exposure.contributions}
        ),
        templates=deps.reviewed_templates,
        clips=ClipStore(),
        proposals=ProposalStore(),
        segmenter=ColorSegmenter(),
    )
    values.update(overrides)
    return OrchestratorServices(**values)


def build_orchestrator(scripts: dict[str, list], services: OrchestratorServices | None = None, config: OrchestratorConfig | None = None):
    fake = ScriptedByModel(scripts)
    services = services or build_services()
    orchestrator = Orchestrator(TracedAgentModel(fake), services, config or OrchestratorConfig())
    return orchestrator, fake, services


def run(orchestrator: Orchestrator, text: str = "hola", scenario: HazardContext | None = None, clip_id: str | None = None):
    trace = TraceStore().create()
    with use_trace(trace):
        result = orchestrator.run(text, trace, scenario=scenario, clip_id=clip_id)
    return result, trace


ATTENTION_SCENARIO = HazardContext(
    enfen_magnitude=EnfenMagnitude.EXTRAORDINARY, senamhi_level=SenamhiLevel.ORANGE, forecast_rain_72h_mm=80.0
)
RAIN_SCENARIO = replace(ATTENTION_SCENARIO, observed_rain_24h_mm=35.0)

__all__ = [name for name in dir() if not name.startswith("_")] + ["AlertTier"]

"""The multi-agent orchestrator over HTTP: runs, live trace (SSE), dispatch proposals and watcher clips.

POST /agent/run                         start a planner run (optionally wait for it)
GET  /agent/trace/{run_id}              full trace: events, totals, result
GET  /agent/trace/{run_id}/stream       the same events as Server-Sent Events, live
GET  /agent/trace/{run_id}/artifact/..  PNGs produced during the run (evacuation card, SAM overlays)
GET  /agent/proposals, POST .../approve, POST .../reject    the operator's explicit click
POST /agent/clips                       register a river-watcher clip for the Watcher agent
GET  /agent/config                      models, budget and providers (no secrets)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Callable, Iterator, Sequence

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from willay.adapters.clip_media import probe_clip
from willay.adapters.traced_models import TracedAgentModel, TracedImage, TracedSegmenter
from willay.api.alert_routes import execute_dispatch
from willay.api.dependencies import AppDependencies
from willay.api.watch_routes import MAX_CLIP_BYTES, MAX_CLIP_SECONDS, render_overlays
from willay.application.orchestration.orchestrator import Orchestrator
from willay.application.orchestration.runtime import (
    OrchestratorServices,
    ProposalError,
    approve_proposal,
)
from willay.application.orchestration.trace import RunTrace, use_trace
from willay.application.rank_sites import RankedSite
from willay.domain.entities import AlertTier, EnfenMagnitude, HazardContext, RainfallAnomaly, SenamhiLevel

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="willay-run")
WAIT_TIMEOUT_S = 300.0


class ScenarioIn(BaseModel):
    """Conditions of the run, set by the operator (or the demo): they override the current hazard context."""

    enfen_magnitude: EnfenMagnitude | None = None
    seasonal_rainfall_anomaly: RainfallAnomaly | None = None
    senamhi_level: SenamhiLevel | None = None
    forecast_rain_72h_mm: float | None = Field(default=None, ge=0)
    observed_rain_24h_mm: float | None = Field(default=None, ge=0)
    water_rise_cm: float | None = Field(default=None, ge=0)
    watcher_report: bool | None = None

    def apply(self, base: HazardContext) -> HazardContext:
        changes = {k: v for k, v in self.model_dump().items() if v is not None}
        from dataclasses import replace

        return replace(base, operator_confirmed=False, **changes)


class AgentRunIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    scenario: ScenarioIn | None = None
    clip_id: str | None = Field(default=None, max_length=80)
    wait: bool = Field(default=False, description="Block until the run ends (tests and simple clients)")


class ApproveIn(BaseModel):
    operator_confirmation_id: str = Field(default="", max_length=80)


class ApprovalError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def approve_and_dispatch(
    deps: AppDependencies,
    find_ranked: Callable[[str], RankedSite],
    proposal_id: str,
    operator_confirmation_id: str | None,
    simulated: bool = False,
) -> dict:
    """The operator's click: approve the proposal (tier re-evaluated by the rules) and dispatch to the synthetic households.

    `simulated=True` is used only by the demo's optional auto-confirm and is written in the trace as such.
    """
    proposal = deps.proposals.get(proposal_id)
    if proposal is None:
        raise ApprovalError(404, f"unknown proposal '{proposal_id}'")
    ranked_site = find_ranked(proposal.site_id)
    try:
        approve_proposal(deps.proposals, proposal_id, operator_confirmation_id, ranked_site.static_risk)
    except ProposalError as error:
        raise ApprovalError(422 if "confirmation id" in str(error) else 409, str(error)) from error
    try:
        dispatch = execute_dispatch(deps, ranked_site, proposal.final_tier or proposal.tier, proposal.operator_confirmation_id)
    except ValueError as refused:
        deps.proposals.reopen(proposal_id)
        raise ApprovalError(409, str(refused)) from refused
    deps.proposals.mark_dispatched(proposal_id, dispatch["dispatch_id"])
    trace = deps.traces.get(proposal.run_id)
    if trace is not None:
        who = "simulated operator click (demo auto-confirm)" if simulated else "operator click"
        trace.emit(
            "operator_action",
            agent="operator",
            result_summary=f"approved {proposal_id} by {who}: {dispatch['tier']} dispatched to {dispatch['households_targeted']} synthetic household(s)",
            detail={"proposal_id": proposal_id, "dispatch_id": dispatch["dispatch_id"], "tier": dispatch["tier"],
                    "operator_confirmation_id": proposal.operator_confirmation_id, "simulated": simulated},
        )
    return {"proposal": proposal.to_dict(), "dispatch": dispatch}


def build_services(
    deps: AppDependencies,
    ranked: Callable[[], Sequence[RankedSite]],
    segmenter=None,
    image=None,
) -> OrchestratorServices:
    """Wire the orchestrator to the app: real data, the Meta adapters wrapped for tracing, and the human-in-the-loop stores."""
    settlements = lambda: list(deps.repository.list_settlements())  # noqa: E731

    def households(site_id: str) -> int:
        site = next((r for r in ranked() if r.site.id == site_id), None)
        if site is None or site.exposure is None:
            return 0
        reached = {c.settlement_id for c in site.exposure.contributions}
        return sum(1 for h in deps.households if h.settlement_id in reached)

    def budget_remaining() -> float | None:
        usage = deps.usage_log
        if usage is None:
            return None
        return float(usage.budget_usd) - float(usage.total_cost())  # type: ignore[attr-defined]

    seg = segmenter if segmenter is not None else (TracedSegmenter(deps.video_segmenter) if deps.video_segmenter is not None else None)
    img = image if image is not None else (TracedImage(deps.image_generator) if deps.image_generator is not None else None)
    return OrchestratorServices(
        ranked=ranked,
        hazard=deps.hazard_scenario,
        official_source=deps.official_source,
        settlements=settlements,
        clips=deps.clips,
        proposals=deps.proposals,
        households=households,
        templates=deps.reviewed_templates,  # type: ignore[arg-type]
        llm=deps.llm,
        image=img,
        segmenter=seg,
        districts=deps.district_collection,
        backtest_results=deps.backtest_results,
        learned_model=deps.learned_model,
        rain_context=deps.rain_context,
        tier_config=deps.tier_config,
        budget_remaining=budget_remaining,
        clip_probe=lambda data, suffix: probe_clip(data, suffix).frames,
        overlay_renderer=render_overlays,
    )


def run_to_trace(orchestrator: Orchestrator, trace: RunTrace, text: str, scenario: HazardContext | None, clip_id: str | None) -> None:
    """Execute a run and close its trace (completed or failed). Never raises."""
    try:
        with use_trace(trace):
            result = orchestrator.run(text, trace, scenario=scenario, clip_id=clip_id)
        trace.finish("completed", result.to_dict())
    except Exception as exc:  # the run ends as failed; the message carries no prompt or key (adapters redact it)
        trace.emit("guardrail", agent="planner", status="error", result_summary=f"run_failed: {type(exc).__name__}")
        trace.finish("failed", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})


def register_orchestration_routes(
    app: FastAPI, deps: AppDependencies, ranked: Callable[[], Sequence[RankedSite]], find_ranked: Callable[[str], RankedSite]
) -> None:
    def make_orchestrator() -> Orchestrator:
        assert deps.agent_model is not None
        model = TracedAgentModel(deps.agent_model, default_model=deps.orchestrator_config.specialist_model)
        return Orchestrator(model, build_services(deps, ranked), deps.orchestrator_config)

    @app.post("/agent/run")
    def agent_run(request: AgentRunIn) -> dict:
        if deps.agent_model is None:
            raise HTTPException(status_code=503, detail="the agent model is not configured (set WILLAY_META_ENABLED=1 and META_API_KEY)")
        scenario = request.scenario.apply(deps.hazard_scenario) if request.scenario else None
        trace = deps.traces.create()
        future = _EXECUTOR.submit(run_to_trace, make_orchestrator(), trace, request.text.strip(), scenario, request.clip_id)
        if request.wait:
            future.result(timeout=WAIT_TIMEOUT_S)
            return {"run_id": trace.run_id, "status": trace.status, "result": trace.result, "totals": trace.totals()}
        return {"run_id": trace.run_id, "status": trace.status}

    def get_trace(run_id: str) -> RunTrace:
        trace = deps.traces.get(run_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return trace

    @app.get("/agent/trace/{run_id}")
    def trace_json(run_id: str) -> dict:
        return get_trace(run_id).snapshot()

    @app.get("/agent/trace/{run_id}/stream")
    def trace_stream(run_id: str) -> StreamingResponse:
        trace = get_trace(run_id)

        def events() -> Iterator[str]:
            for event in trace.iter_events():
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/agent/trace/{run_id}/artifact/{name}")
    def trace_artifact(run_id: str, name: str) -> Response:
        artifact = get_trace(run_id).artifacts.get(name)
        if artifact is None:
            raise HTTPException(status_code=404, detail=f"unknown artifact '{name}'")
        return Response(content=artifact[1], media_type=artifact[0])

    # --- proposals --------------------------------------------------------------------------------------------

    @app.get("/agent/proposals")
    def proposals() -> dict:
        return {"proposals": [p.to_dict() for p in deps.proposals.list()]}

    def note_in_trace(run_id: str, summary: str, **detail: object) -> None:
        trace = deps.traces.get(run_id)
        if trace is not None:
            trace.emit("operator_action", agent="operator", result_summary=summary, detail=detail)

    @app.post("/agent/proposals/{proposal_id}/approve")
    def approve(proposal_id: str, request: ApproveIn) -> dict:
        try:
            return approve_and_dispatch(deps, find_ranked, proposal_id, request.operator_confirmation_id)
        except ApprovalError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error

    @app.post("/agent/proposals/{proposal_id}/reject")
    def reject(proposal_id: str) -> dict:
        if deps.proposals.get(proposal_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown proposal '{proposal_id}'")
        try:
            proposal = deps.proposals.reject(proposal_id)
        except ProposalError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        note_in_trace(proposal.run_id, f"rejected {proposal_id} by the operator", proposal_id=proposal_id)
        return {"proposal": proposal.to_dict()}

    # --- clips + config -----------------------------------------------------------------------------------------

    @app.post("/agent/clips")
    def upload_clip(
        file: Annotated[UploadFile, File(description="MP4 or MOV, at most 15 s, no people in frame")],
        no_people_confirmed: Annotated[bool, Form(description="Must be true: clips may be sent to a third-party model")] = False,
    ) -> dict:
        if not no_people_confirmed:
            raise HTTPException(status_code=400, detail="confirm that the clip shows no people (no_people_confirmed=true)")
        content_type = (file.content_type or "").lower()
        if content_type not in ("video/mp4", "video/quicktime"):
            raise HTTPException(status_code=415, detail="only MP4 or MOV clips are accepted")
        video = file.file.read(MAX_CLIP_BYTES + 1)
        if len(video) > MAX_CLIP_BYTES:
            raise HTTPException(status_code=413, detail="clip is larger than 40 MB")
        suffix = ".mov" if content_type == "video/quicktime" else ".mp4"
        try:
            info = probe_clip(video, suffix)
        except Exception as exc:
            raise HTTPException(status_code=422, detail="could not decode the clip as MP4/MOV video") from exc
        if info.frames <= 0 or info.duration_s <= 0:
            raise HTTPException(status_code=422, detail="the clip has no decodable frames")
        if info.duration_s > MAX_CLIP_SECONDS + 0.25:
            raise HTTPException(status_code=422, detail=f"clip is {info.duration_s:.1f} s long; the limit is {MAX_CLIP_SECONDS:.0f} s")
        clip_id = f"clip-{len(deps.clips.ids()) + 1}"
        deps.clips.add(clip_id, video, content_type)
        return {"clip_id": clip_id, "seconds": round(info.duration_s, 2), "frames": info.frames}

    @app.get("/agent/config")
    def config() -> dict:
        cfg = deps.orchestrator_config
        usage = deps.usage_log
        return {
            "live": deps.agent_model is not None,
            "planner_model": cfg.planner_model,
            "planner_fallback_model": cfg.planner_fallback_model,
            "specialist_model": cfg.specialist_model,
            "run_budget_usd": cfg.run_budget_usd,
            "budget": None
            if usage is None
            else {"spent_usd": round(float(usage.total_cost()), 4), "limit_usd": float(usage.budget_usd)},  # type: ignore[attr-defined]
            "calls_mode": deps.calls_mode,
            "clips": deps.clips.ids(),
            "tiers": [t.value for t in AlertTier if t is not AlertTier.NONE],
        }

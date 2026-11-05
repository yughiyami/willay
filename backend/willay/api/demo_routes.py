"""Demo mode: the scripted end-to-end scenario, live (Meta models) or offline (recorded turns), on one shared trace.

POST /demo/run     start the demo ({"mode": "offline" | "live", "auto_confirm": false})
GET  /demo/status  what is available and whether a demo is running

The demo is the real orchestrator running a fixed script. Live, the planner and specialists are the Meta models.
Offline, the same code replays recorded model turns and recorded SAM output (no network, no cost). The operator's
confirmation is a real click (POST /agent/proposals/{id}/approve); `auto_confirm` performs that same call on the
operator's behalf and writes "simulated" in the trace.
"""

from __future__ import annotations

import base64
import functools
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Literal, Sequence

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from willay.adapters.clip_media import ColorSegmenter
from willay.adapters.replay_models import ReplayAgentModel, ReplayImage, ReplayMismatch, ReplaySegmenter
from willay.adapters.synthetic_clip import SyntheticClipSpec, render_clip, write_mp4
from willay.adapters.traced_models import TracedAgentModel, TracedImage, TracedSegmenter
from willay.api.dependencies import AppDependencies
from willay.api.orchestration_routes import ApprovalError, approve_and_dispatch, build_services
from willay.application.dispatch_alerts import record_response
from willay.application.orchestration.demo_script import (
    DEMO_CLIP_ID,
    DEMO_CONFIG_NOTE,
    DEMO_OPERATOR_ID,
    DEMO_TIER_CONFIG,
    DEMO_WATER_PROMPT,
    STAGES,
    pick_demo_site,
)
from willay.application.orchestration.orchestrator import Orchestrator
from willay.application.orchestration.trace import RunTrace, use_trace
from willay.application.rank_sites import RankedSite
from willay.domain.dispatch import ResponseKind


class DemoRunIn(BaseModel):
    mode: Literal["offline", "live"] = "offline"
    auto_confirm: bool = False


class DemoFailed(RuntimeError):
    """The scripted scenario could not complete; the message is shown to the audience."""


@functools.lru_cache(maxsize=1)
def demo_clip_bytes() -> bytes:
    """A fake rising-water scene (red post in a blue river, +45 cm over 6 s, 60 frames): synthetic, no people, deterministic."""
    spec = SyntheticClipSpec(seconds=6.0, fps=10, marker_height_cm=100.0, rise_cm=45.0)
    frames, _ = render_clip(spec)
    with tempfile.TemporaryDirectory(prefix="willay-demo-") as directory:
        path = Path(directory) / "demo.mp4"
        write_mp4(frames, spec.fps, path)
        return path.read_bytes()


class DemoRunner:
    def __init__(self, deps: AppDependencies, ranked: Callable[[], Sequence[RankedSite]], find_ranked: Callable[[str], RankedSite]) -> None:
        self._deps = deps
        self._ranked = ranked
        self._find_ranked = find_ranked
        self._lock = threading.Lock()
        self._trace: RunTrace | None = None
        self._mode = ""

    # --- status --------------------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._trace is not None and not self._trace.finished

    def status(self) -> dict:
        return {
            "running": self.running,
            "run_id": self._trace.run_id if self._trace else None,
            "mode": self._mode,
            "offline_available": self._deps.demo_recording is not None,
            "live_available": self._deps.agent_model is not None,
            "calls_mode": self._deps.calls_mode,
        }

    # --- start ---------------------------------------------------------------------------------------------------

    def start(self, mode: str, auto_confirm: bool) -> RunTrace:
        with self._lock:
            if self.running:
                raise HTTPException(status_code=409, detail="a demo is already running")
            if mode == "live" and self._deps.agent_model is None:
                raise HTTPException(status_code=503, detail="live demo needs WILLAY_META_ENABLED=1 and META_API_KEY")
            if mode == "offline" and not self._deps.demo_recording:
                raise HTTPException(status_code=503, detail="no demo recording found (run scripts/record_demo.py once with live Meta)")
            if self._pick_site() is None:
                raise HTTPException(status_code=409, detail="no site with demo households downstream is available")
            trace = self._deps.traces.create()
            self._trace, self._mode = trace, mode
        threading.Thread(target=self._execute, args=(trace, mode, auto_confirm), name="willay-demo", daemon=True).start()
        return trace

    # --- internals --------------------------------------------------------------------------------------------------

    def _pick_site(self) -> RankedSite | None:
        return pick_demo_site(self._ranked(), self._deps.households)

    def _reset_state(self) -> None:
        deps = self._deps
        for target in (deps.ledger, deps.messaging):
            reset = getattr(target, "reset", None)
            if callable(reset):
                reset()
        for chat_channel in (deps.webchat, deps.telegram):  # alert ids bound to chats mean nothing after the ledger reset
            if chat_channel is not None:
                chat_channel.reset_bindings()
        deps.proposals.clear()
        deps.clips.add(DEMO_CLIP_ID, demo_clip_bytes(), "video/mp4")

    def _models(self, mode: str, site_id: str):
        deps = self._deps
        config = deps.orchestrator_config
        if mode == "live":
            assert deps.agent_model is not None
            return TracedAgentModel(deps.agent_model, default_model=config.specialist_model), None, None
        recording = deps.demo_recording or {}
        model = TracedAgentModel(ReplayAgentModel(recording, site_id, pace_s=deps.demo_pace_s), default_model=config.specialist_model, replayed=True)
        segmentations = recording.get("segmentations")
        if segmentations:
            segmenter = TracedSegmenter(ReplaySegmenter(segmentations), replayed=True)
        else:
            segmenter = TracedSegmenter(ColorSegmenter(), model_label="sam-3.1 (offline color stand-in)", standin=True)
        card = recording.get("card_png_base64")
        image = TracedImage(ReplayImage(base64.b64decode(card)), replayed=True) if card else None
        return model, segmenter, image

    def _step(self, trace: RunTrace, key: str, number: int, total: int, title: str, **detail: object) -> None:
        trace.emit("step", agent="demo", result_summary=f"{number}/{total} {title}", detail={"key": key, "number": number, "of": total, "title": title, **detail})
        if self._deps.demo_pace_s > 0:
            time.sleep(self._deps.demo_pace_s)

    def _execute(self, trace: RunTrace, mode: str, auto_confirm: bool) -> None:
        try:
            with use_trace(trace):
                result = self._script(trace, mode, auto_confirm)
            trace.finish("completed", result)
        except ReplayMismatch as mismatch:
            trace.emit("guardrail", agent="demo", status="error", result_summary="recording no longer matches this build")
            trace.finish("failed", {"error": f"the recording does not match this build ({mismatch}); record it again with scripts/record_demo.py"})
        except DemoFailed as failure:
            trace.emit("guardrail", agent="demo", status="error", result_summary=f"demo stopped: {failure}")
            trace.finish("failed", {"error": str(failure)})
        except Exception as exc:
            trace.emit("guardrail", agent="demo", status="error", result_summary=f"demo failed: {type(exc).__name__}")
            trace.finish("failed", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

    def _script(self, trace: RunTrace, mode: str, auto_confirm: bool) -> dict:
        deps = self._deps
        site = self._pick_site()
        assert site is not None
        self._reset_state()
        model, segmenter, image = self._models(mode, site.site.id)
        services = build_services(deps, self._ranked, segmenter=segmenter, image=image)
        services.tier_config = DEMO_TIER_CONFIG
        services.water_prompt = DEMO_WATER_PROMPT
        orchestrator = Orchestrator(model, services, deps.orchestrator_config)

        total = len(STAGES) + 4
        trace.emit(
            "step", agent="demo",
            result_summary=f"demo {mode}: {site.site.name}",
            detail={"key": "start", "mode": mode, "site_id": site.site.id, "site_name": site.site.name, "note": DEMO_CONFIG_NOTE},
        )
        proposals: list[dict] = []
        for number, stage in enumerate(STAGES, 1):
            self._step(trace, stage.key, number, total, stage.title_es, scenario_stage=stage.key)
            outcome = orchestrator.run(stage.prompt(site.site.id, site.site.name), trace, scenario=stage.scenario, clip_id=stage.clip_id)
            proposals = [p for p in outcome.proposals if p["tier"] == "EVACUATION"] or proposals
        if not proposals:
            raise DemoFailed("the planner did not prepare an evacuation proposal; see the trace for what each agent returned")
        proposal_id = proposals[-1]["id"]

        self._step(trace, "operator", len(STAGES) + 1, total, "El operador debe confirmar la evacuación", proposal_id=proposal_id, awaiting_operator=True)
        if auto_confirm:
            try:
                approve_and_dispatch(deps, self._find_ranked, proposal_id, DEMO_OPERATOR_ID, simulated=True)
            except ApprovalError as error:
                raise DemoFailed(f"the simulated operator click was refused: {error}") from error
        else:
            deadline = time.monotonic() + deps.demo_confirm_timeout_s
            while time.monotonic() < deadline:
                status = deps.proposals.get(proposal_id).status  # type: ignore[union-attr]
                if status != "awaiting_operator":
                    break
                time.sleep(0.1)
            status = deps.proposals.get(proposal_id).status  # type: ignore[union-attr]
            if status == "awaiting_operator":
                trace.emit("guardrail", agent="demo", result_summary="operator_timeout: nobody confirmed, nothing was sent")
                return {"mode": mode, "site_id": site.site.id, "proposal_id": proposal_id, "outcome": "operator_timeout", "sent": 0}
            if status == "rejected":
                return {"mode": mode, "site_id": site.site.id, "proposal_id": proposal_id, "outcome": "rejected_by_operator", "sent": 0}

        alerts = list(deps.ledger.list_alerts())
        self._step(trace, "dispatch", len(STAGES) + 2, total, "Envío a hogares sintéticos (mensaje y llamada simulada)", households=len(alerts))
        target = next((a for a in alerts if a.response is not ResponseKind.NEEDS_HELP), alerts[0] if alerts else None)
        if target is None:
            raise DemoFailed("the dispatch reached no household")
        record_response(target.id, ResponseKind.NEEDS_HELP, "whatsapp", deps.ledger)
        self._step(
            trace, "household_reply", len(STAGES) + 3, total, "Un hogar responde 2 (necesito ayuda): se abre un caso", alert_id=target.id, simulated_reply=True,
        )
        open_cases = [c for c in deps.ledger.list_cases() if c.status == "open"]
        counts: dict[str, int] = {}
        for alert in deps.ledger.list_alerts():
            key = alert.response.value if alert.response else "AWAITING"
            counts[key] = counts.get(key, 0) + 1
        self._step(trace, "done", total, total, "Fin del escenario", open_cases=len(open_cases))
        return {
            "mode": mode,
            "site_id": site.site.id,
            "proposal_id": proposal_id,
            "outcome": "dispatched",
            "households": len(alerts),
            "counts": counts,
            "open_cases": len(open_cases),
            "cost_usd": trace.totals()["cost_usd"],
        }


def register_demo_routes(
    app: FastAPI, deps: AppDependencies, ranked: Callable[[], Sequence[RankedSite]], find_ranked: Callable[[str], RankedSite]
) -> DemoRunner:
    runner = DemoRunner(deps, ranked, find_ranked)

    @app.post("/demo/run")
    def demo_run(request: DemoRunIn) -> dict:
        trace = runner.start(request.mode, request.auto_confirm)
        return {"run_id": trace.run_id, "mode": request.mode, "auto_confirm": request.auto_confirm}

    @app.get("/demo/card")
    def demo_card() -> Response:
        card = (deps.demo_recording or {}).get("card_png_base64")
        if not card:
            raise HTTPException(status_code=404, detail="no recorded card")
        return Response(content=base64.b64decode(card), media_type="image/png")

    @app.get("/demo/status")
    def demo_status() -> dict:
        return runner.status()

    return runner

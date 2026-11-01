"""Public signals over HTTP: unverified news and web reports of recent hazard events, context only.

GET  /signals?district=        current signals, aggregated per district with source links (mode: live | simulated | off)
POST /signals/refresh          live: search the open web, extract with the model, geocode, store (cooldown; needs the env flags)
POST /signals/simulated        {"enabled": bool} show the offline, clearly fake set for Demo mode (never mixed with live signals)

Signals are labelled "señales públicas no verificadas". Nothing here can change an alert tier or start a dispatch.
"""

from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from willay.api.dependencies import AppDependencies
from willay.application.public_signals import build_gazetteer, refresh_public_signals


class SimulatedIn(BaseModel):
    enabled: bool


def register_signals_routes(app: FastAPI, deps: AppDependencies) -> None:
    busy = threading.Lock()
    gazetteer_cache: dict[str, object] = {}

    def gazetteer():
        if "value" not in gazetteer_cache:
            gazetteer_cache["value"] = build_gazetteer(list(deps.repository.list_settlements()), deps.district_collection)
        return gazetteer_cache["value"]

    @app.get("/signals")
    def signals(district: str | None = Query(default=None, max_length=80, description="District name or 6-digit ubigeo")) -> dict:
        return deps.signals.snapshot(district)

    @app.post("/signals/simulated")
    def simulated(request: SimulatedIn) -> dict:
        if deps.signals.enabled:
            raise HTTPException(status_code=409, detail="live public signals are enabled; the simulated set is never mixed with them")
        if not deps.signals.simulation_available:
            raise HTTPException(status_code=404, detail="no simulated signals fixture is loaded")
        deps.signals.set_simulated(request.enabled)
        return deps.signals.snapshot()

    @app.post("/signals/refresh")
    def refresh() -> dict:
        hub = deps.signals
        if not hub.enabled or deps.signal_search is None:
            reason = hub.note or "set WILLAY_SIGNALS_ENABLED=1 and TAVILY_API_KEY"
            raise HTTPException(status_code=503, detail=f"public signals are off ({reason})")
        if deps.extractor is None:
            raise HTTPException(status_code=503, detail="the extraction model is not configured (set WILLAY_META_ENABLED=1 and META_API_KEY)")
        age = hub.seconds_since_refresh()
        if age is not None and age < deps.signals_cooldown_s:
            raise HTTPException(
                status_code=429,
                detail=f"public signals were refreshed {int(age)} s ago; wait {deps.signals_cooldown_s:.0f} s between refreshes to limit search and model cost",
            )
        if not busy.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a refresh is already running")
        try:
            report = refresh_public_signals(
                deps.signal_search,
                deps.extractor,
                gazetteer(),  # type: ignore[arg-type]
                hub,
                now=hub.now(),
                max_queries=deps.signals_max_queries,
            )
        finally:
            busy.release()
        return {"report": report.to_dict(), "signals": hub.snapshot()}

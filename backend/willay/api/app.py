"""FastAPI composition root. Wires adapters (real data or fixtures) to the use cases."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from willay.api.dependencies import AppDependencies, default_dependencies
from willay.api.schemas import BacktestIn, EvaluateAlertIn
from willay.application.compose_alert_message import AlertMessageInput, compose_alert_message
from willay.application.district_backtest import recompute_top_k
from willay.application.evaluate_alert_tier import evaluate_alert_tier
from willay.application.rank_sites import RankedSite, rank_sites
from willay.application.run_backtest import run_backtest
from willay.domain.entities import AlertTier, HazardContext
from willay.domain.tiers import TierEvaluation

LOCAL_WEB_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",  # vite preview
    "http://127.0.0.1:4173",
]


def _hazard_to_dict(ctx: HazardContext) -> dict:
    return {
        "enfen_magnitude": ctx.enfen_magnitude.value,
        "seasonal_rainfall_anomaly": ctx.seasonal_rainfall_anomaly.value,
        "senamhi_level": ctx.senamhi_level.value,
        "forecast_rain_72h_mm": ctx.forecast_rain_72h_mm,
        "observed_rain_24h_mm": ctx.observed_rain_24h_mm,
        "water_rise_cm": ctx.water_rise_cm,
        "watcher_report": ctx.watcher_report,
        "operator_confirmed": ctx.operator_confirmed,
    }


def _evaluation_to_dict(evaluation: TierEvaluation) -> dict:
    return {
        "tier": evaluation.tier.value,
        "triggered": [t.value for t in evaluation.triggered],
        "reasons": list(evaluation.reasons),
        "requires_operator_confirmation": evaluation.tier is AlertTier.PENDING_CONFIRMATION,
    }


def create_app(deps: AppDependencies | None = None) -> FastAPI:
    deps = deps or default_dependencies()
    app = FastAPI(
        title="Willay Relaves API",
        version="0.2.0",
        description=(
            "El Nino early warning for tailings sites in Peru. Alerts complement official "
            "SENAMHI/INDECI/ENFEN alerts and never replace them. Alert tiers are deterministic; "
            "language models only rephrase or explain."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=LOCAL_WEB_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    cache: dict[bool, list[RankedSite]] = {}

    def ranked_sites(basin_only: bool | None = None) -> list[RankedSite]:
        """Ranking is deterministic for a fixed scenario, so it is computed once per filter."""
        only = deps.basin_only if basin_only is None else basin_only
        if only not in cache:
            cache[only] = rank_sites(
                deps.repository,
                deps.hazard_scenario,
                exposure_config=deps.exposure_config,
                intakes=list(deps.repository.list_water_intakes()),
                basin_only=only,
            )
        return cache[only]

    def find_ranked(site_id: str) -> RankedSite:
        for ranked in ranked_sites(False):
            if ranked.site.id == site_id:
                return ranked
        raise HTTPException(status_code=404, detail=f"unknown site '{site_id}'")

    def flow_path_feature(site_id: str) -> dict | None:
        if site_id in deps.flow_path_features:
            return dict(deps.flow_path_features[site_id])
        site = deps.repository.get_site(site_id)
        if site is None or site.flow_path is None:
            return None
        points = site.flow_path.points
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[p.lon, p.lat] for p in points]},
            "properties": {
                "site_id": site_id,
                "name": site.name,
                "length_m": site.flow_path.total_length_m,
                "cumulative_distance_m": [p.cumulative_distance_m for p in points],
            },
        }

    def compose_for(ranked: RankedSite, tier: AlertTier) -> dict | None:
        if tier is AlertTier.NONE or ranked.exposure is None or ranked.exposure.nearest is None:
            return None
        nearest = ranked.exposure.nearest
        settlement = next((s for s in deps.repository.list_settlements() if s.id == nearest.settlement_id), None)
        message = compose_alert_message(
            AlertMessageInput(
                tier=tier,
                site_name=ranked.site.name,
                distance_km=nearest.along_path_m / 1000.0,
                safe_point=settlement.safe_point if settlement else None,
                official_source=deps.official_source,
            ),
            llm=deps.llm,
        )
        return {
            "tier": message.tier.value,
            "text": message.text,
            "audience": message.audience,
            "rephrased": message.rephrased,
        }

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "synthetic_data": deps.synthetic, "data_mode": deps.data_mode}

    @app.get("/sites")
    def list_sites(
        basin_only: bool | None = Query(default=None, description="Only sites inside the Rimac basin polygon"),
        detail: bool = Query(default=False, description="Include reached settlements per site"),
    ) -> dict:
        return {
            "synthetic": deps.synthetic,
            "data_mode": deps.data_mode,
            "disclaimer": deps.disclaimer,
            "official_source": deps.official_source,
            "hazard_context": _hazard_to_dict(deps.hazard_scenario),
            "sites": [r.to_dict(include_exposure_detail=detail) for r in ranked_sites(basin_only)],
        }

    @app.get("/sites/{site_id}")
    def get_site(site_id: str) -> dict:
        ranked = find_ranked(site_id)
        evaluation = evaluate_alert_tier(deps.hazard_scenario, ranked.static_risk)
        data = ranked.to_dict()
        data["alert"] = _evaluation_to_dict(evaluation)
        data["hazard_context"] = _hazard_to_dict(deps.hazard_scenario)
        data["flow_path"] = flow_path_feature(site_id)
        data["static_inputs"] = dict(deps.site_static_inputs.get(site_id, {}))
        return data

    @app.get("/flowpaths")
    def flow_paths() -> dict:
        features = [f for site in deps.repository.list_sites() if (f := flow_path_feature(site.id)) is not None]
        return {"type": "FeatureCollection", "features": features}

    @app.get("/layers/pasivos")
    def pasivos_layer() -> dict:
        return {
            "secondary": True,
            "inventory_year": 2011,
            "note": "MINEM mining liabilities inventory (Oct 2011): referential and outdated; no volume or risk data",
            "items": [p.to_dict() for p in deps.pasivos],
        }

    @app.get("/layers/intakes")
    def intakes_layer() -> dict:
        return {
            "note": "Approximate OpenStreetMap locations; verify with SEDAPAL before any use",
            "items": [
                {
                    "id": i.id,
                    "name": i.name,
                    "lon": i.lon,
                    "lat": i.lat,
                    "operator": i.operator,
                    "kind": i.kind,
                    "approximate": i.approximate,
                }
                for i in deps.repository.list_water_intakes()
            ],
        }

    @app.get("/districts")
    def districts() -> dict:
        if deps.district_collection is None:
            raise HTTPException(
                status_code=404, detail="district data is only available with real data (run scripts/build_district_features.py)"
            )
        return dict(deps.district_collection)

    @app.post("/alerts/evaluate")
    def evaluate_alert(request: EvaluateAlertIn) -> dict:
        ranked = find_ranked(request.site_id) if request.site_id is not None else None
        static_risk = request.site_static_risk
        if static_risk is None:
            assert ranked is not None  # guaranteed by request validation
            static_risk = ranked.static_risk
        config = request.thresholds.to_config() if request.thresholds else None
        evaluation = evaluate_alert_tier(request.to_context(), static_risk, config)
        body = _evaluation_to_dict(evaluation)
        body["site_static_risk"] = static_risk
        body["message"] = compose_for(ranked, evaluation.tier) if ranked is not None else None
        return body

    def backtest_response(event: str | None, top_k: tuple[int, ...] | None) -> dict:
        if deps.synthetic:
            report = run_backtest(deps.backtest_dataset, top_k_pcts=top_k or (10, 20)).to_dict()  # type: ignore[arg-type]
            report["data_mode"] = deps.data_mode
            return report
        if deps.backtest_results is None:
            raise HTTPException(status_code=404, detail="no stored backtest (run scripts/run_backtest.py)")
        events = deps.backtest_results["events"]
        if event is None:
            return {"data_mode": deps.data_mode, "primary_gate": deps.backtest_results.get("primary_gate"), "events": events}
        if event not in events:
            raise HTTPException(status_code=404, detail=f"unknown event '{event}' (available: {sorted(events)})")
        result = recompute_top_k(events[event], top_k) if top_k else dict(events[event])
        result["data_mode"] = deps.data_mode
        return result

    @app.get("/backtest")
    def backtest_get(event: str | None = Query(default=None, description="2017 or 2023")) -> dict:
        return backtest_response(event, None)

    @app.post("/backtest")
    def backtest_post(request: BacktestIn | None = None, event: str | None = Query(default=None)) -> dict:
        top_k = tuple(request.top_k_pcts) if request else None
        return backtest_response(event, top_k)

    return app


app = create_app()

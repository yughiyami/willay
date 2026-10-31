"""FastAPI composition root. Wires adapters (fixtures by default) to the use cases."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from willay.adapters.fixture_loader import load_backtest_fixture, load_rimac_fixture
from willay.adapters.in_memory import InMemorySiteRepository
from willay.api.schemas import BacktestIn, EvaluateAlertIn
from willay.application.compose_alert_message import AlertMessageInput, compose_alert_message
from willay.application.evaluate_alert_tier import evaluate_alert_tier
from willay.application.rank_sites import RankedSite, rank_sites
from willay.application.run_backtest import BacktestDataset, run_backtest
from willay.domain.entities import AlertTier, HazardContext
from willay.domain.tiers import TierEvaluation
from willay.ports.llm import LLMPort
from willay.ports.site_repository import SiteRepositoryPort

LOCAL_WEB_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


@dataclass
class AppDependencies:
    repository: SiteRepositoryPort
    hazard_scenario: HazardContext
    official_source: str
    backtest_dataset: BacktestDataset
    synthetic: bool
    disclaimer: str
    llm: LLMPort | None = None


def default_dependencies() -> AppDependencies:
    fixture = load_rimac_fixture()
    return AppDependencies(
        repository=InMemorySiteRepository(fixture.sites, fixture.settlements),
        hazard_scenario=fixture.hazard_scenario,
        official_source=fixture.official_source,
        backtest_dataset=load_backtest_fixture(),
        synthetic=fixture.synthetic,
        disclaimer=fixture.disclaimer,
    )


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
        version="0.1.0",
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

    def ranked_sites() -> list[RankedSite]:
        return rank_sites(deps.repository, deps.hazard_scenario)

    def find_ranked(site_id: str) -> RankedSite:
        for ranked in ranked_sites():
            if ranked.site.id == site_id:
                return ranked
        raise HTTPException(status_code=404, detail=f"unknown site '{site_id}'")

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
        return {"status": "ok", "synthetic_data": deps.synthetic}

    @app.get("/sites")
    def list_sites() -> dict:
        return {
            "synthetic": deps.synthetic,
            "disclaimer": deps.disclaimer,
            "hazard_context": _hazard_to_dict(deps.hazard_scenario),
            "sites": [r.to_dict(include_exposure_detail=False) for r in ranked_sites()],
        }

    @app.get("/sites/{site_id}")
    def get_site(site_id: str) -> dict:
        ranked = find_ranked(site_id)
        evaluation = evaluate_alert_tier(deps.hazard_scenario, ranked.static_risk)
        data = ranked.to_dict()
        data["alert"] = _evaluation_to_dict(evaluation)
        data["hazard_context"] = _hazard_to_dict(deps.hazard_scenario)
        return data

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

    @app.post("/backtest")
    def backtest(request: BacktestIn | None = None) -> dict:
        top_k = tuple(request.top_k_pcts) if request else (10, 20)
        return run_backtest(deps.backtest_dataset, top_k_pcts=top_k).to_dict()

    return app


app = create_app()

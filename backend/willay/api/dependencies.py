"""Composition of the API dependencies: bundled synthetic fixtures or the real data folder.

WILLAY_DATA_MODE=real (default) reads WILLAY_DATA_DIR (default ../willay-data);
WILLAY_DATA_MODE=synthetic uses the invented fixtures bundled in backend/fixtures (used by tests).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from willay.adapters.alert_ledger import InMemoryAlertLedger
from willay.adapters.asterisk_ari import AsteriskAriAdapter, MissingAsteriskConfigError
from willay.adapters.data_dir import DataDir, resolve_data_dir
from willay.adapters.fixture_loader import load_backtest_fixture, load_rimac_fixture
from willay.adapters.geodata.emergencies import (
    TARGET_HAZARDS,
    filter_emergencies,
    first_emergency_by_district,
    load_indeci_csv,
)
from willay.adapters.geodata.loader import load_real_world
from willay.adapters.geodata.timeseries import load_chirps_csv, load_enfen_csv
from willay.adapters.households_fixture import load_households
from willay.adapters.in_memory import InMemorySiteRepository
from willay.adapters.simulated_call import SimulatedCallAdapter
from willay.adapters.web_whatsapp import WebWhatsAppSimulator
from willay.adapters.safe_points_file import FILE_NAME as SAFE_POINTS_FILE
from willay.adapters.safe_points_file import load_proposed_safe_points
from willay.adapters.reviewed_templates import FileReviewedTemplates
from willay.application.district_backtest import STANDARD_EVENTS
from willay.application.orchestration.runtime import (
    DEFAULT_FALLBACK_MODEL,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_SPECIALIST_MODEL,
    ClipStore,
    OrchestratorConfig,
    ProposalStore,
)
from willay.application.orchestration.trace import TraceStore
from willay.application.run_backtest import BacktestDataset
from willay.domain.dispatch import Household
from willay.domain.district_score import DEFAULT_WEIGHTS, DistrictFeatures, percentile_ranks, score_districts
from willay.domain.entities import HazardContext, RainfallAnomaly
from willay.domain.tiers import TierConfig
from willay.domain.exposure import ExposureConfig
from willay.ports.agent import AgentModelPort, StructuredExtractionPort
from willay.ports.alert_ledger import AlertLedgerPort
from willay.ports.calls import CallPort
from willay.ports.image import ImagePort, TranscriptionPort
from willay.ports.llm import LLMPort
from willay.ports.messaging import MessagingPort
from willay.ports.segmentation import VideoSegmentationPort
from willay.ports.site_repository import SiteRepositoryPort

ENV_MODE = "WILLAY_DATA_MODE"
ENV_ANOMALY = "WILLAY_SEASONAL_ANOMALY"
ENV_TEMPLATES_DIR = "WILLAY_REVIEWED_TEMPLATES_DIR"
ENV_META_ENABLED = "WILLAY_META_ENABLED"
ENV_CALLS_ENABLED = "WILLAY_CALLS_ENABLED"
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "reviewed"
REAL_DISCLAIMER = (
    "Real open data (INGEMMET, ANA, OSM, Copernicus DEM). Tailings risk attributes are sparse, water intakes are "
    "approximate OpenStreetMap locations and flow paths come from a 30 m DEM: verify before any operational use. "
    "Willay Relaves complements the official SENAMHI, INDECI and ENFEN alerts and never replaces them."
)


@dataclass
class AppDependencies:
    repository: SiteRepositoryPort
    hazard_scenario: HazardContext
    official_source: str
    backtest_dataset: BacktestDataset | None
    synthetic: bool
    disclaimer: str
    llm: LLMPort | None = None
    data_mode: str = "synthetic"
    basin_only: bool = False
    exposure_config: ExposureConfig | None = None
    flow_path_features: Mapping[str, Mapping] = field(default_factory=dict)
    site_static_inputs: Mapping[str, Mapping] = field(default_factory=dict)
    pasivos: Sequence = ()
    district_collection: Mapping | None = None
    backtest_results: Mapping | None = None
    learned_model: Mapping | None = None
    reviewed_templates: object | None = None
    # Live Meta capabilities. None means "not configured": the matching endpoints answer 503.
    video_segmenter: VideoSegmentationPort | None = None
    agent_model: AgentModelPort | None = None
    transcriber: TranscriptionPort | None = None
    image_generator: ImagePort | None = None
    extractor: StructuredExtractionPort | None = None
    derived_dir: Path | None = None
    hazard_notes: Sequence[str] = ()
    # Alert loop. Messages go to the web WhatsApp simulator and calls are simulated unless explicitly enabled.
    households: Sequence[Household] = ()
    messaging: MessagingPort = field(default_factory=WebWhatsAppSimulator)
    calls: CallPort = field(default_factory=SimulatedCallAdapter)
    calls_mode: str = "simulated"
    ledger: AlertLedgerPort = field(default_factory=InMemoryAlertLedger)
    # Multi-agent orchestrator: models, run traces, dispatch proposals and the river-watcher clips.
    orchestrator_config: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    traces: TraceStore = field(default_factory=TraceStore)
    proposals: ProposalStore = field(default_factory=ProposalStore)
    clips: ClipStore = field(default_factory=ClipStore)
    usage_log: object | None = None  # the Meta usage log (spend and budget), when live Meta is wired
    rain_context: Mapping | None = None
    tier_config: TierConfig | None = None


def orchestrator_config_from_env(environ: Mapping[str, str] | None = None) -> OrchestratorConfig:
    env = os.environ if environ is None else environ
    defaults = OrchestratorConfig()

    def number(name: str, default: float) -> float:
        try:
            return float(env[name]) if env.get(name) else default
        except ValueError:
            return default

    return OrchestratorConfig(
        planner_model=env.get("WILLAY_PLANNER_MODEL") or DEFAULT_PLANNER_MODEL,
        planner_fallback_model=env.get("WILLAY_PLANNER_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL,
        specialist_model=env.get("WILLAY_SPECIALIST_MODEL") or DEFAULT_SPECIALIST_MODEL,
        min_planner_budget_usd=number("WILLAY_PLANNER_MIN_BUDGET_USD", defaults.min_planner_budget_usd),
        run_budget_usd=number("WILLAY_RUN_BUDGET_USD", defaults.run_budget_usd),
    )


def _rain_context(data: DataDir) -> Mapping | None:
    path = data.processed("chirps_v2_rimac_daily_mm.csv")
    if not path.exists():
        return None
    series = load_chirps_csv(path)
    if not series:
        return None
    last = max(r.date for r in series)
    window = [r for r in series if (last - r.date).days < 30]
    return {
        "source": "CHIRPS v2 daily series (5 km), mean over the sierra cells of the Rimac bbox",
        "series_start": min(r.date for r in series).isoformat(),
        "last_date": last.isoformat(),
        "trailing_30d_sierra_mean_mm": round(sum(r.sierra_mean_mm for r in window), 1),
        "caveat": "5 km satellite estimate: it underestimates local peaks and is a wetness proxy, not a trigger",
    }


def reviewed_templates_from_env(environ: Mapping[str, str] | None = None) -> FileReviewedTemplates:
    env = os.environ if environ is None else environ
    return FileReviewedTemplates(env.get(ENV_TEMPLATES_DIR) or DEFAULT_TEMPLATES_DIR)


def synthetic_dependencies() -> AppDependencies:
    fixture = load_rimac_fixture()
    households = load_households()
    return AppDependencies(
        repository=InMemorySiteRepository(fixture.sites, fixture.settlements),
        hazard_scenario=fixture.hazard_scenario,
        official_source=fixture.official_source,
        backtest_dataset=load_backtest_fixture(),
        synthetic=fixture.synthetic,
        disclaimer=fixture.disclaimer,
        reviewed_templates=reviewed_templates_from_env(),
        households=households,
        calls=SimulatedCallAdapter({h.phone: h.demo_call_outcome for h in households}),
    )


def _current_hazard(bulletins, anomaly: RainfallAnomaly) -> tuple[HazardContext, str]:
    if not bulletins:
        return HazardContext(seasonal_rainfall_anomaly=anomaly), "no ENFEN communique available"
    latest = max(bulletins, key=lambda b: b.date)
    context = HazardContext(enfen_magnitude=latest.magnitude, seasonal_rainfall_anomaly=anomaly)
    return context, f"ENFEN Comunicado Oficial N°{latest.number} ({latest.date.isoformat()})"


def _verified_enfen_ingest(data: DataDir, bulletins) -> RainfallAnomaly | None:
    """Seasonal rainfall outlook from `derived/enfen_hazard.json` (scripts/ingest_enfen.py), or None.

    Used only when the ingest is verified (no flags), refers to the latest curated communique and carries an
    outlook. Anything else falls back to the configured default.
    """
    path = data.derived("enfen_hazard.json")
    if not path.exists() or not bulletins:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        latest = max(bulletins, key=lambda b: b.date)
        if not record.get("verified") or record.get("number") != latest.number or not record.get("seasonal_rainfall_anomaly"):
            return None
        return RainfallAnomaly(record["seasonal_rainfall_anomaly"])
    except (ValueError, OSError, TypeError, KeyError):
        return None


def _learned_model(data: DataDir) -> Mapping | None:
    path = data.derived("learned_model.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _district_collection(world, data: DataDir, learned: Mapping | None = None) -> Mapping | None:
    features_path = data.derived("district_features.json")
    if not features_path.exists():
        return None
    district_features = json.loads(features_path.read_text(encoding="utf-8"))["districts"]
    polygons_path = data.derived("district_polygons.geojson")
    geometry = {}
    if polygons_path.exists():
        for f in json.loads(polygons_path.read_text(encoding="utf-8"))["features"]:
            geometry[f["properties"]["ubigeo"]] = f["geometry"]

    scores = {
        s.ubigeo: s
        for s in score_districts(
            [
                DistrictFeatures(
                    ubigeo=ubigeo,
                    name=f["name"],
                    mm_high_share=f["mm_high_share"],
                    mean_slope_deg=f["mean_slope_deg"],
                    exposed_population=f["exposed_population"].get("all"),
                    tailings_upstream=f["tailings_upstream"],
                )
                for ubigeo, f in district_features.items()
            ]
        )
    }
    cenepred = {d.ubigeo: d for d in world.cenepred}

    indeci_path = data.processed("indeci_emergencias_distritos_bbox_2003_2023.csv")
    windows: dict[str, dict[str, date]] = {}
    counts: dict[str, dict[str, int]] = {}
    if indeci_path.exists():
        emergencies = load_indeci_csv(indeci_path)
        for key, event in STANDARD_EVENTS.items():
            inside = filter_emergencies(emergencies, TARGET_HAZARDS, event.label_start, event.label_end)
            windows[key] = first_emergency_by_district(inside)
            counts[key] = {}
            for e in inside:
                counts[key][e.ubigeo] = counts[key].get(e.ubigeo, 0) + 1

    learned_probability = {u: d["probability"] for u, d in (learned or {}).get("districts", {}).items()}
    learned_rank = {u: r for u, r in zip(sorted(learned_probability), percentile_ranks([learned_probability[u] for u in sorted(learned_probability)]))}

    features = []
    for ubigeo, feats in district_features.items():
        score = scores[ubigeo]
        c = cenepred.get(ubigeo)
        features.append(
            {
                "type": "Feature",
                "geometry": geometry.get(ubigeo),
                "properties": {
                    "ubigeo": ubigeo,
                    "name": feats["name"],
                    "static_score": score.value,
                    "components": {k: v for k, v in score.components.items()},
                    "missing_components": list(score.missing),
                    "features": {k: v for k, v in feats.items() if k != "tailings_upstream_ids"},
                    "cenepred": None
                    if c is None
                    else {
                        "baseline_only": True,
                        "scenario": "El Nino 2027 (SENAMHI IT N°07-2026)",
                        "flood_risk_value": c.flood_risk_value,
                        "flood_risk_level": c.flood_risk_level,
                        "mm_risk_value": c.mm_risk_value,
                        "mm_risk_level": c.mm_risk_level,
                        "population_2017": c.population_2017,
                    },
                    "learned": None
                    if ubigeo not in learned_probability
                    else {
                        "probability": learned_probability[ubigeo],
                        "percentile_rank": learned_rank[ubigeo],
                        "scope": "typical_season_propensity",
                    },
                    "emergencies": {
                        key: {
                            "count": counts.get(key, {}).get(ubigeo, 0),
                            "first_date": windows[key][ubigeo].isoformat() if ubigeo in windows.get(key, {}) else None,
                        }
                        for key in STANDARD_EVENTS
                    },
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "scoring": {
            "weights": dict(DEFAULT_WEIGHTS),
            "method": "percentile ranks of INGEMMET susceptibility share, mean slope, exposed population and "
            "upstream tailings, combined with fixed a-priori weights; no outcome or CENEPRED input",
            "emergency_hazards": list(TARGET_HAZARDS),
        },
        "features": features,
    }


def real_dependencies(data: DataDir | None = None, environ: Mapping[str, str] | None = None) -> AppDependencies:
    env = os.environ if environ is None else environ
    data = data or DataDir(resolve_data_dir(env))
    world = load_real_world(data)
    enfen_path = data.processed("enfen_timeline.csv")
    bulletins = load_enfen_csv(enfen_path) if enfen_path.exists() else []
    anomaly = RainfallAnomaly(env.get(ENV_ANOMALY, RainfallAnomaly.NORMAL.value))
    ingested = _verified_enfen_ingest(data, bulletins) if ENV_ANOMALY not in env else None
    if ingested is not None:
        anomaly = ingested
    hazard, source = _current_hazard(bulletins, anomaly)
    hazard_notes: tuple[str, ...] = ()
    if ingested is not None:
        hazard_notes = (
            f"seasonal rainfall outlook '{anomaly.value}' read from the ENFEN communique by a language model and "
            "verified against the document text (scripts/ingest_enfen.py)",
        )

    households = load_households(environ=env)
    learned = _learned_model(data)
    results_path = data.derived("backtest_results.json")
    backtest_results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else None

    proposed = load_proposed_safe_points(data.derived(SAFE_POINTS_FILE))
    settlements = tuple(
        dataclasses.replace(s, proposed_safe_point=proposed[s.id]) if s.id in proposed else s for s in world.settlements
    )
    return AppDependencies(
        repository=InMemorySiteRepository(world.sites, settlements, world.intakes),
        hazard_scenario=hazard,
        official_source=source,
        backtest_dataset=None,
        synthetic=False,
        disclaimer=REAL_DISCLAIMER,
        data_mode="real",
        basin_only=True,
        # Flow paths are capped at 80 km, so exposure is evaluated over the whole path.
        exposure_config=ExposureConfig(cutoff_m=80_000.0),
        flow_path_features=world.flow_path_geojson,
        site_static_inputs={
            site_id: {
                **features,
                "flow_path_stop_reason": world.flow_path_meta.get(site_id, {}).get("stop_reason"),
                "flow_path_length_m": world.flow_path_meta.get(site_id, {}).get("length_m"),
            }
            for site_id, features in world.site_features.items()
        },
        pasivos=world.pasivos,
        district_collection=_district_collection(world, data, learned),
        learned_model=learned,
        rain_context=_rain_context(data),
        backtest_results=backtest_results,
        reviewed_templates=reviewed_templates_from_env(env),
        derived_dir=data.derived(),
        hazard_notes=hazard_notes,
        households=households,
        calls=SimulatedCallAdapter({h.phone: h.demo_call_outcome for h in households}),
    )


def attach_meta_from_env(deps: AppDependencies, environ: Mapping[str, str] | None = None) -> AppDependencies:
    """Wire the live Meta Model API into every capability, only when WILLAY_META_ENABLED=1.

    The adapter reads META_API_KEY from `.env`/the environment at runtime (never printed) and logs every call
    to <data>/derived/meta_usage.jsonl under a spend budget. A missing key leaves the app running without it.
    """
    env = os.environ if environ is None else environ
    if env.get(ENV_META_ENABLED, "0").strip() != "1":
        return deps
    from willay.adapters import meta_model_api  # local import: the adapter is optional at start-up

    try:
        adapter = meta_model_api.load_adapter_from_env()
    except meta_model_api.MissingApiKeyError:
        return deps
    deps.agent_model = adapter
    deps.transcriber = adapter
    deps.video_segmenter = adapter
    deps.image_generator = adapter
    deps.extractor = adapter
    deps.usage_log = getattr(adapter, "usage_log", None)
    deps.orchestrator_config = orchestrator_config_from_env(env)
    return deps


def attach_calls_from_env(deps: AppDependencies, environ: Mapping[str, str] | None = None) -> AppDependencies:
    """Real phone calls through Asterisk ARI, only when WILLAY_CALLS_ENABLED=1 AND Asterisk is fully configured.

    Anything else keeps the simulated adapter (no call is ever placed). Even with real calls, the adapter refuses
    numbers outside WILLAY_CALL_ALLOWLIST. Only variable NAMES are ever reported, never values.
    """
    env = os.environ if environ is None else environ
    if env.get(ENV_CALLS_ENABLED, "0").strip() != "1":
        return deps
    try:
        adapter = AsteriskAriAdapter.from_env(env)
    except MissingAsteriskConfigError as missing:
        deps.calls_mode = f"simulated (calls enabled but Asterisk is not configured: {missing})"
        return deps
    except ValueError as invalid:
        deps.calls_mode = f"simulated (calls enabled but the Asterisk configuration is invalid: {invalid})"
        return deps
    deps.calls = adapter
    deps.calls_mode = f"asterisk (allow-list: {len(adapter.allowlist)} number(s))"
    return deps


def default_dependencies() -> AppDependencies:
    mode = os.environ.get(ENV_MODE, "real").strip().lower()
    if mode == "synthetic":
        return attach_calls_from_env(attach_meta_from_env(synthetic_dependencies()))
    if mode == "real":
        return attach_calls_from_env(attach_meta_from_env(real_dependencies()))
    raise ValueError(f"{ENV_MODE} must be 'real' or 'synthetic', got '{mode}'")


__all__ = [
    "AppDependencies",
    "attach_calls_from_env",
    "attach_meta_from_env",
    "default_dependencies",
    "orchestrator_config_from_env",
    "real_dependencies",
    "synthetic_dependencies",
]

"""Offline run: district-level backtest for the Jan-Apr 2017 and Mar 2023 events.

Labels: the district had at least one INDECI emergency of a rain-driven hazard type
(huayco, flujo de detritos, deslizamiento, inundacion, lluvia intensa) in the label window.
Models: our static district score, the CENEPRED El Nino 2027 scenario (baseline only) and a
population-only baseline, all evaluated identically.

Reads  <data>/derived/district_features.json plus the processed CENEPRED, INDECI, CHIRPS and ENFEN files.
Writes <data>/derived/backtest_results.json

Run:  .venv/Scripts/python -I scripts/build_district_features.py   (once)
      .venv/Scripts/python -I scripts/run_backtest.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.geodata.emergencies import (  # noqa: E402
    TARGET_HAZARDS,
    filter_emergencies,
    first_emergency_by_district,
    load_indeci_csv,
)
from willay.adapters.geodata.hazards import load_cenepred_csv  # noqa: E402
from willay.adapters.geodata.timeseries import load_chirps_csv, load_enfen_csv  # noqa: E402
from willay.application.district_backtest import (  # noqa: E402
    STANDARD_EVENTS,
    DistrictRow,
    PreparationGate,
    ScenarioBaseline,
    build_model_scores,
    run_district_backtest,
)
from willay.domain.district_score import DistrictFeatures  # noqa: E402
from willay.domain.entities import EnfenMagnitude  # noqa: E402

EVENTS = tuple(STANDARD_EVENTS.values())

# Fixed before looking at any result. The primary gate is weak_rain80.
GATES = (
    PreparationGate(
        "strict_strong_rain80",
        EnfenMagnitude.STRONG,
        min_window_rain_mm=80.0,
        description="The PREPARATION rule as written: ENFEN magnitude >= strong, rain accumulation proxy, district rank >= 0.5",
    ),
    PreparationGate(
        "weak_enfen_only",
        EnfenMagnitude.WEAK,
        description="ENFEN magnitude >= weak (alert/watch with a forecast event) and district rank >= 0.5; no rainfall condition",
    ),
    PreparationGate("weak_rain40", EnfenMagnitude.WEAK, min_window_rain_mm=40.0, description="weak + 30-day sierra rain >= 40 mm"),
    PreparationGate(
        "weak_rain80",
        EnfenMagnitude.WEAK,
        min_window_rain_mm=80.0,
        description="weak + 30-day sierra rain >= 80 mm (primary, fixed a priori; threshold not calibrated)",
    ),
    PreparationGate("weak_rain120", EnfenMagnitude.WEAK, min_window_rain_mm=120.0, description="weak + 30-day sierra rain >= 120 mm"),
)
PRIMARY_GATE = "weak_rain80"
TOP_K = (10, 20)


def main() -> None:
    data = DataDir(resolve_data_dir())
    files = {
        "features": data.derived("district_features.json"),
        "cenepred": data.processed("cenepred_er_fen2027_distritos_bbox.csv"),
        "indeci": data.processed("indeci_emergencias_distritos_bbox_2003_2023.csv"),
        "chirps": data.processed("chirps_v2_rimac_daily_mm.csv"),
        "enfen": data.processed("enfen_timeline.csv"),
    }
    data.require(*files.values())

    district_features = json.loads(files["features"].read_text(encoding="utf-8"))["districts"]
    cenepred = {d.ubigeo: d for d in load_cenepred_csv(files["cenepred"])}
    universe = sorted(u for u in district_features if u in cenepred)
    emergencies = load_indeci_csv(files["indeci"])
    bulletins = load_enfen_csv(files["enfen"])
    rain = load_chirps_csv(files["chirps"])

    rows = [DistrictRow(u, district_features[u]["name"], cenepred[u].population_2017) for u in universe]
    baselines = [
        ScenarioBaseline(u, cenepred[u].name, cenepred[u].population_2017, cenepred[u].flood_risk_value, cenepred[u].mm_risk_value)
        for u in universe
    ]

    results = {}
    for event in EVENTS:
        year = event.key
        features = [
            DistrictFeatures(
                ubigeo=u,
                name=district_features[u]["name"],
                mm_high_share=district_features[u]["mm_high_share"],
                mean_slope_deg=district_features[u]["mean_slope_deg"],
                exposed_population=district_features[u]["exposed_population"][year],
                tailings_upstream=district_features[u]["tailings_upstream"],
            )
            for u in universe
        ]
        in_window = filter_emergencies(emergencies, TARGET_HAZARDS, event.label_start, event.label_end)
        first = {u: d for u, d in first_emergency_by_district(in_window).items() if u in set(universe)}
        result = run_district_backtest(
            event,
            rows,
            build_model_scores(features, baselines),
            first,
            bulletins,
            rain,
            GATES,
            PRIMARY_GATE,
            TOP_K,
        )
        result["label_definition"] = {
            "hazard_types": list(TARGET_HAZARDS),
            "n_records_in_window": len([e for e in in_window if e.ubigeo in set(universe)]),
            "source": "INDECI-SINPAD emergency database 2003-2023 (district + report date)",
        }
        results[year] = result

        print(f"\n=== {year}: {result['n_positive']}/{result['n_districts']} districts with an emergency ===")
        for name, m in result["models"].items():
            cap = m["top_k_capture"]
            print(
                f"{name:22s} AUC={m['auc'] if m['auc'] is None else round(m['auc'], 3)} "
                f"CI95={[round(x, 2) for x in m['auc_ci95']] if m['auc_ci95'] else None} "
                f"top10%={cap['10'] if cap['10'] is None else round(cap['10'], 3)} "
                f"top20%={cap['20'] if cap['20'] is None else round(cap['20'], 3)} "
                f"(max20%={round(m['max_top_k_capture']['20'], 3)})"
            )
        for gate, by_model in result["lead_time"].items():
            first_day = result["gates"][gate]["first_eligible_day"]
            parts = [
                f"{name}: n={s['n']} med={s['median']} (p25 {s['p25']}, p75 {s['p75']}) none={s['n_without_lead']}"
                for name, s in by_model.items()
                if name in ("willay_static", "cenepred_baseline", "population_baseline")
            ]
            print(f"  gate {gate} (first eligible {first_day})")
            for part in parts:
                print(f"     {part}")

    payload = {
        "generated_on": date.today().isoformat(),
        "method": "docs/BACKTEST.md",
        "primary_gate": PRIMARY_GATE,
        "events": results,
    }
    target = data.write_derived("backtest_results.json", json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"\nwrote {target}")


if __name__ == "__main__":
    main()

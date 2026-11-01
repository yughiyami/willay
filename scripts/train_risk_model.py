"""Offline run: train the learned district risk model on seasons 2003-2016 and test it on 2017 and 2023.

Rows: one per (district, season Jan-Apr). Label: at least one INDECI rain-driven emergency in the season.
Features: the static district inputs (INGEMMET susceptibility share, mean slope, exposed population, tailings upstream).
Model: L2 logistic regression (numpy); the L2 strength is chosen by leave-one-season-out AUC on the training seasons.
Test labels are exactly the backtest labels (2017: Jan-Apr, 2023: Mar), so every model is compared on the same districts.

Reads  <data>/derived/district_features.json, backtest_results.json, processed INDECI and CENEPRED files.
Writes <data>/derived/learned_model.json  (coefficients, per-district probability, AUC with bootstrap CI, calibration)

Run:  .venv/Scripts/python -I scripts/train_risk_model.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.geodata.emergencies import TARGET_HAZARDS, filter_emergencies, load_indeci_csv  # noqa: E402
from willay.adapters.geodata.hazards import load_cenepred_csv  # noqa: E402
from willay.adapters.learned_risk import (  # noqa: E402
    DEFAULT_L2_GRID,
    DistrictInputs,
    brier_score,
    build_season_rows,
    select_l2,
    train_model,
)
from willay.application.district_backtest import STANDARD_EVENTS  # noqa: E402
from willay.domain.backtest_metrics import bootstrap_auc_ci, roc_auc  # noqa: E402
from willay.domain.district_score import rank_average  # noqa: E402

TRAIN_YEARS = tuple(range(2003, 2017))
TEST_EVENTS = ("2017", "2023")
EXPOSURE_KEY = "2017"  # ANA critical points designated before 2017: nothing from the test seasons leaks in


def main() -> None:
    data = DataDir(resolve_data_dir())
    files = {
        "features": data.derived("district_features.json"),
        "backtest": data.derived("backtest_results.json"),
        "cenepred": data.processed("cenepred_er_fen2027_distritos_bbox.csv"),
        "indeci": data.processed("indeci_emergencias_distritos_bbox_2003_2023.csv"),
    }
    data.require(*files.values())

    district_features = json.loads(files["features"].read_text(encoding="utf-8"))["districts"]
    cenepred = {d.ubigeo for d in load_cenepred_csv(files["cenepred"])}
    universe = sorted(u for u in district_features if u in cenepred)
    inputs = [
        DistrictInputs(
            ubigeo=u,
            mm_high_share=district_features[u]["mm_high_share"],
            mean_slope_deg=district_features[u]["mean_slope_deg"],
            exposed_population=district_features[u]["exposed_population"][EXPOSURE_KEY],
            tailings_upstream=district_features[u]["tailings_upstream"],
        )
        for u in universe
    ]
    emergencies = load_indeci_csv(files["indeci"])

    def positives(start: date, end: date) -> set[str]:
        return {e.ubigeo for e in filter_emergencies(emergencies, TARGET_HAZARDS, start, end) if e.ubigeo in set(universe)}

    labels_by_year = {y: positives(date(y, 1, 1), date(y, 4, 30)) for y in TRAIN_YEARS}
    for key in TEST_EVENTS:
        event = STANDARD_EVENTS[key]
        labels_by_year[int(key)] = positives(event.label_start, event.label_end)
    years = [*TRAIN_YEARS, *(int(k) for k in TEST_EVENTS)]
    rows = build_season_rows(inputs, labels_by_year, years)

    train_rows = [r for r in rows if r.year in TRAIN_YEARS]
    _, cv_report = select_l2(train_rows, DEFAULT_L2_GRID)
    model = train_model(rows, TRAIN_YEARS, [int(k) for k in TEST_EVENTS], DEFAULT_L2_GRID)

    backtest = json.loads(files["backtest"].read_text(encoding="utf-8"))["events"]
    probabilities = dict(zip(universe, model.predict_proba(inputs)))
    evaluation = {}
    for key in TEST_EVENTS:
        label_set = labels_by_year[int(key)]
        labels = [1 if u in label_set else 0 for u in universe]
        scores = [probabilities[u] for u in universe]
        auc = roc_auc(scores, labels)
        low, high = bootstrap_auc_ci(scores, labels)
        table = {d["ubigeo"]: d["scores"] for d in backtest[key]["districts"]}
        ensemble_scores = rank_average(probabilities, {u: table[u]["cenepred_baseline"] for u in universe})
        ens_auc = roc_auc([ensemble_scores[u] for u in universe], labels)
        ens_low, ens_high = bootstrap_auc_ci([ensemble_scores[u] for u in universe], labels)
        evaluation[key] = {
            "n_districts": len(universe),
            "n_positive": sum(labels),
            "base_rate": sum(labels) / len(labels),
            "learned": {"auc": auc, "auc_ci95": [low, high], "brier": brier_score(scores, labels), "mean_predicted": sum(scores) / len(scores)},
            "ensemble_learned_cenepred": {"auc": ens_auc, "auc_ci95": [ens_low, ens_high]},
            "reference": {
                name: {"auc": m["auc"], "auc_ci95": m["auc_ci95"]}
                for name, m in backtest[key]["models"].items()
                if name in ("willay_static", "cenepred_baseline", "ensemble_static_cenepred", "population_baseline")
            },
            # Calibration of the training base rate against the realised rate: shows how far a "probability" can be trusted.
            "calibration": {"train_base_rate": model.train_base_rate, "observed_rate": sum(labels) / len(labels)},
        }
        print(
            f"{key}: learned AUC {auc:.3f} [{low:.2f}, {high:.2f}]  ensemble+CENEPRED {ens_auc:.3f}  "
            f"static {evaluation[key]['reference']['willay_static']['auc']:.3f}  cenepred {evaluation[key]['reference']['cenepred_baseline']['auc']:.3f}  "
            f"brier {evaluation[key]['learned']['brier']:.3f} mean_p {evaluation[key]['learned']['mean_predicted']:.2f} observed {sum(labels) / len(labels):.2f}"
        )

    payload = {
        "generated_on": date.today().isoformat(),
        "method": "docs/BACKTEST.md (Learned model)",
        "split": {"train_seasons": [TRAIN_YEARS[0], TRAIN_YEARS[-1]], "test_seasons": [int(k) for k in TEST_EVENTS]},
        "l2_cv_auc": {str(k): v for k, v in cv_report.items()},
        "model": model.to_dict(),
        "evaluation": evaluation,
        "districts": {
            u: {"probability": probabilities[u], "name": district_features[u]["name"]} for u in universe
        },
        "caveat": (
            "Features are static, so the model learns how often a district reported rain-driven emergencies in Jan-Apr seasons "
            "2003-2016 given its physical inputs. The probability is a relative propensity for a typical season, not a forecast "
            "for a given year; it does not use ENFEN or rain."
        ),
    }
    target = data.write_derived("learned_model.json", json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"coefficients (standardised): {dict(zip(model.to_dict()['features'], [round(w, 3) for w in model.weights]))}")
    print(f"l2={model.l2}  cv={cv_report}")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()

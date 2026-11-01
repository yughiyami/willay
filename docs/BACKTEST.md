# District backtest: Jan-Apr 2017 and Mar 2023

Reproduce (reads `WILLAY_DATA_DIR`, default `../willay-data`; writes only to `derived/`):

```bash
.venv/Scripts/python -I scripts/build_flowpaths.py
.venv/Scripts/python -I scripts/build_site_features.py
.venv/Scripts/python -I scripts/build_district_features.py
.venv/Scripts/python -I scripts/run_backtest.py     # -> derived/backtest_results.json
.venv/Scripts/python -I scripts/train_risk_model.py # -> derived/learned_model.json (see Learned model)
```

Served by `GET /backtest?event=2017|2023` and `POST /backtest?event=...` (the POST recomputes the top-k
metrics from the stored district table for any `top_k_pcts`).

## Method

**Unit and labels.** The 78 districts of the study bbox. A district is positive if INDECI-SINPAD
recorded at least one emergency of a rain-driven type (HUAYCO, FLUJO DE DETRITOS, DESLIZAMIENTO,
INUNDACION, LLUVIA INTENSA) in the window: 1 Jan - 30 Apr 2017 (52 positive, 359 records) or
1 - 31 Mar 2023 (48 positive, 56 records). DERRUMBE DE CERRO, ALUD, EROSION, fires and quakes are excluded.

**Our static district score** (`domain/district_score.py`). Percentile ranks of four inputs, combined
with fixed a-priori weights that were not fitted to any label:

| Input | Weight | Source |
|---|---|---|
| Share of high and very-high mass-movement susceptibility area | 0.35 | INGEMMET susceptibility (area by district) |
| Mean slope of land cells | 0.20 | Copernicus GLO-30 |
| Population in settlements within 500 m of an IGN river or 1 km of an ANA critical point | 0.30 | ANA `Pob_rev`, IGN rivers, ANA critical points |
| Tailings sites whose D8 flow path crosses the district | 0.15 | INGEMMET inventory + our flow paths |

The CENEPRED scenario is **not** an input. To limit leakage, ANA critical points designated in or after the
event year are dropped for that event (1 point before 2017, 52 before 2023).

**Baselines, evaluated identically** (same labels, metrics and gate):
CENEPRED El Nino 2027 district risk (mean of flood and mass-movement risk values; the two components are also
reported separately) and a population-only baseline (census 2017 population).

**Ensemble (added later, all baselines kept).** `ensemble_static_cenepred` is the equal-weight rank-average of our
static score and the CENEPRED mean (percentile rank of each, averaged; `domain/district_score.rank_average`). The weights
(0.5 / 0.5) were fixed before the run and nothing was fitted to the labels. It is a *comparison of a combination*, not a
clean out-of-sample model: its CENEPRED half is a 2027 scenario, not a hindcast (limitation 5).

**Metrics.** ROC AUC (Mann-Whitney) with a 95% percentile-bootstrap interval (1000 resamples of districts);
top-k% capture (share of positive districts inside the top k% scored; ties share slots), shown with its
ceiling (`max` = slots / positives) because with a 60-67% base rate a top-20% list of 16 districts cannot capture
more than about a third of the positives; precision in the top-k.

**Lead time.** Days from the first PREPARATION-eligible date to the district's first emergency, per positive
district. Eligibility uses the existing `evaluate_tier` PREPARATION rule day by day with only information
published up to that day: ENFEN magnitude from the latest communique (curated timeline; first magnitude word
in the forecast text; neutral while "No activo"), a rainfall condition, and the district's score percentile
rank in the role of the site static risk (threshold 0.5). Percentile ranks are used for all models so the gate is
comparable. Districts whose emergency came before eligibility, or that the model did not flag, count under "none".

The existing rule needs ENFEN magnitude >= strong and a seasonal rainfall anomaly forecast of above normal.
No seasonal outlook exists in the data folder and there is no CHIRPS climatology, so the anomaly is proxied
by an absolute accumulation (trailing 30-day sum of the CHIRPS sierra mean). The gates, fixed before any result was seen:

| Gate | ENFEN | Rain proxy |
|---|---|---|
| `strict_strong_rain80` (rule as written) | >= strong | 30-day >= 80 mm |
| `weak_enfen_only` | >= weak | none |
| `weak_rain40` / **`weak_rain80` (primary)** / `weak_rain120` | >= weak | 30-day >= 40 / 80 / 120 mm |

## Results

Base rate 2017: 52/78 = 0.67. Base rate 2023: 48/78 = 0.62.

| Event | Model | AUC [95% CI] | Top-10% | Top-20% | Ceiling 20% |
|---|---|---|---|---|---|
| 2017 | **Willay static** | **0.93** [0.86, 0.98] | 0.15 | 0.31 | 0.31 |
| 2017 | CENEPRED (mean) | 0.77 [0.66, 0.87] | 0.13 | 0.27 | 0.31 |
| 2017 | Ensemble (rank-avg static + CENEPRED) | 0.90 [0.80, 0.97] | 0.15 | 0.29 | 0.31 |
| 2017 | CENEPRED mass movement only | 0.91 [0.83, 0.97] | 0.15 | 0.29 | 0.31 |
| 2017 | CENEPRED flood only | 0.37 [0.26, 0.50] | 0.13 | 0.23 | 0.31 |
| 2017 | Population only | 0.25 [0.15, 0.37] | 0.12 | 0.19 | 0.31 |
| 2023 | **Willay static** | **0.63** [0.49, 0.75] | 0.10 | 0.23 | 0.33 |
| 2023 | CENEPRED (mean) | 0.80 [0.70, 0.89] | 0.17 | 0.31 | 0.33 |
| 2023 | Ensemble (rank-avg static + CENEPRED) | 0.75 [0.63, 0.85] | 0.13 | 0.29 | 0.33 |
| 2023 | CENEPRED mass movement only | 0.73 [0.61, 0.84] | 0.13 | 0.29 | 0.33 |
| 2023 | CENEPRED flood only | 0.63 [0.50, 0.75] | 0.13 | 0.29 | 0.33 |
| 2023 | Population only | 0.52 [0.40, 0.65] | 0.15 | 0.27 | 0.33 |

Lead time in days (median, p25-p75, n districts with a lead / n without), by gate:

| Event | Gate (first eligible day) | Willay static | CENEPRED | Population | Ensemble |
|---|---|---|---|---|---|
| 2017 | strict (never) | none (0/52) | none | none | none |
| 2017 | weak_enfen_only (2017-01-24) | 9 d, 5.8-25.8, n=20 / 32 | 40 d, 8-50, n=25 / 27 | 50 d, 8-51, n=15 / 37 | 9 d, 2-47.5, n=24 / 28 |
| 2017 | **weak_rain80 (2017-02-13, primary)** | 23.5 d, 17.8-29.3, n=6 / 46 | 30 d, 29-31, n=13 / 39 | 30.5 d, 30-31, n=10 / 42 | 29 d, 27-30, n=9 / 43 |
| 2017 | weak_rain120 (never) | none | none | none | none |
| 2023 | strict (never) | none (0/48) | none | none | none |
| 2023 | weak (all variants, 2023-03-16) | 6 d, 5.5-10.5, n=3 / 45 | 5 d, 5-5.5, n=3 / 45 | 6 d, n=1 / 47 | 5 d, 5-5, n=2 / 46 |

## Reading the numbers honestly

- **2017: our static score separates well (AUC 0.93), clearly above CENEPRED's mean (0.77) and about equal
  to CENEPRED mass-movement alone (0.91; intervals overlap).** It is partly an echo of INGEMMET susceptibility,
  which both rely on. Top-20% capture equals the attainable ceiling for ours; every model is close to it.
- **2023: we are weaker (AUC 0.63, interval touches 0.5) and CENEPRED is better (0.80).** One event could be luck in
  either direction; we do not claim the 2017 result generalises. A population-only list has no skill in 2017
  (AUC below 0.5: dense desert-coast districts did not have huaycos) and about none in 2023.
- **The PREPARATION rule as written (ENFEN >= strong) would not have fired in either event**: ENFEN forecast
  weak to moderate in 2017 and weak (strong only from May) in 2023. Any lead time exists only under the relaxed
  gates, which are our assumption, not an official rule. Rain thresholds are not calibrated.
- **Lead time is mostly short or absent.** In 2023 the ENFEN alert (16 Mar) came after the peak emergencies (14-15 Mar):
  only 3 of 48 districts have any lead, at 5-6 days. In 2017 the alert (24 Jan / 2 Feb) preceded the March peak
  by weeks, but many districts had their first emergency in January-February, before eligibility; a gate that waits for
  80 mm shortens the lead further. ENFEN status is a coarse, basin-wide, months-scale trigger.
- **Ensemble: it hedges, it does not win.** In 2017 it sits between our score and CENEPRED (AUC 0.90 vs 0.93 and 0.77),
  in 2023 between CENEPRED and ours (0.75 vs 0.80 and 0.63). Its mean AUC over the two events (0.82) is the highest of the
  three, but its worst event (0.75) is not better than CENEPRED's worst (0.77), and every interval overlaps: with two events
  and 78 districts this is not a distinguishable gain. Top-k capture and lead times are in between too (primary gate 2017:
  29 d for 9 districts vs 23.5 d for 6 with our score alone, 30 d for 13 with CENEPRED). The honest claim is only "averaging
  ranks avoided the worst single-model result in each event"; we did not tune the weights to get it. Do not read it as
  evidence that the combination generalises, and remember its CENEPRED half is a scenario, not a hindcast.
- Top-k capture is bounded by the high base rate; precision and AUC are more informative here.
- Only two events and 78 districts: intervals are wide and metric differences below about 0.1 AUC are not distinguishable.

## Learned model (temporal split): an honest negative result

`scripts/train_risk_model.py` trains a small tabular model on the same static district inputs and tests it on later seasons
(`backend/willay/adapters/learned_risk.py`, numpy, unit-tested; output `derived/learned_model.json`, served by `GET /learned-model`
and as `learned` inside `GET /districts`).

- **Rows and labels.** One row per (district, season): 78 districts x 14 training seasons (Jan-Apr 2003-2016) = 1,092 rows.
  Label: at least one INDECI rain-driven emergency (same hazard types as above) in the season. **Temporal split**: train on 2003-2016
  only; test on 2017 (Jan-Apr) and 2023 (Mar), with exactly the labels used by the backtest above. The code refuses a split where a
  training season is not strictly before every test season.
- **Features** (the inputs of the static score): INGEMMET high-susceptibility share, mean slope, log(1 + exposed population),
  log(1 + tailings upstream). The exposure feature uses ANA critical points designated before 2017. Imputation (median), scaling and
  the L2 strength are fitted on the training seasons only (L2 chosen by leave-one-season-out AUC over {0.1, 1, 10, 100}; 10 won).
- **Model.** L2 logistic regression (Newton / IRLS, intercept not penalised). No deep model: see below.

| Event | Model | AUC [95% bootstrap CI] |
|---|---|---|
| 2017 | **Learned (L2 logistic, train 2003-2016)** | **0.55** [0.41, 0.68] |
| 2017 | Willay static (a-priori weights) | 0.93 [0.86, 0.98] |
| 2017 | CENEPRED (mean) | 0.77 [0.66, 0.87] |
| 2017 | Ensemble (static + CENEPRED) | 0.90 [0.80, 0.97] |
| 2017 | Ensemble (learned + CENEPRED) | 0.71 [0.57, 0.82] |
| 2023 | **Learned (L2 logistic, train 2003-2016)** | **0.55** [0.42, 0.69] |
| 2023 | Willay static (a-priori weights) | 0.63 [0.49, 0.75] |
| 2023 | CENEPRED (mean) | 0.80 [0.70, 0.89] |
| 2023 | Ensemble (static + CENEPRED) | 0.75 [0.63, 0.85] |
| 2023 | Ensemble (learned + CENEPRED) | 0.72 [0.60, 0.84] |

**Reading it honestly.**

- The learned model does **not** beat the a-priori static score in 2017 (0.55 vs 0.93, intervals do not overlap) nor CENEPRED in 2023
  (0.55 vs 0.80); against the static score in 2023 it is about equal (0.55 vs 0.63, overlapping intervals). Its own interval includes
  0.5 in both events: no demonstrated skill on the temporal split.
- **Why.** The label distribution shifts hard between train and test: only 2-14 of 78 districts report a rain-driven emergency in an
  ordinary Jan-Apr season (training base rate 9%), against 52 of 78 in 2017 and 48 of 78 in 2023. Ordinary seasons are
  reporting-driven (populous districts report), so the fitted coefficients lean on exposed population (+0.52 standardised) and ignore
  susceptibility (-0.01) and slope (-0.06). Those are exactly the inputs that separate districts in an El Nino season. The model learned
  "who reports" in normal years, not "who is hit" in extreme years.
- **Calibration.** The mean predicted probability is 0.09 in both test seasons while the observed rate is 0.67 and 0.62 (Brier 0.54 and
  0.51). So the per-district `probability` shown in the console is a **relative propensity for a typical season**, not a probability for
  an El Nino season; the console shows it with its percentile rank and the label `typical_season_propensity`. We did not rescale it with
  test-season base rates (that would be leakage).
- **No deep learning, on purpose.** There are 78 districts, four features and about 1,100 strongly dependent rows (the same static
  features repeat in every season), with a regime shift between train and test. A neural network would have nothing more to learn than a
  linear model on this evidence and would overfit label noise; the failure above is a data/regime problem, not a model-capacity problem,
  so a bigger model could not fix it and a "better" AUC would be unfalsifiable. We therefore ship the linear baseline and the negative
  result, and keep the a-priori static score as the primary district score.
- What would change the conclusion: event-dated landslide inventories with coordinates, SENAMHI/CHIRPS covariates per season (so the model
  can learn the interaction "wet season x susceptibility"), and more El Nino seasons than two.

## Limitations

1. **CHIRPS (5 km) underestimates local peaks**: the 14-17 Mar 2017 Chosica huaycos show only 3-5 mm basin mean.
   It acts as a seasonal/antecedent wetness proxy, not a trigger. The 2023 series starts on 1 Feb.
2. **No event-dated landslide inventory with coordinates.** INGEMMET inventory `FECHA` is a survey date, so
   labels are district-day INDECI records only; the backtest cannot say where inside a district or which tailings
   site failed. It evaluates district susceptibility to the event, not tailings failure.
3. **INDECI labels are district-day reports** (report date, not event date; preliminary figures; no coordinates).
   A district with a large urban area can be positive from a small local event. Records exist only to 2023.
4. **Possible leakage in INGEMMET susceptibility**: the national model was built with historical inventories that
   probably include the 2017 events; the 2017 AUC may be optimistic. Slope and susceptibility are correlated.
   ANA critical points are filtered by designation year, but their designation reflects earlier events.
5. **CENEPRED is a 2027 scenario** (SENAMHI IT N 07-2026), not a hindcast of 2017 or 2023: it is a comparison
   baseline for "an existing district risk product", not a like-for-like competitor.
6. Population exposure uses ANA settlement points; metropolitan Lima appears as large aggregated points, so
   "near a river" is crude there. Households are not published.
7. Weights and the 0.5 rank threshold are a-priori choices; sensitivity to them was not explored beyond the gate table.
8. Flow paths come from a 30 m DSM (bridges and buildings can bend them); the `tailings upstream` input depends on them.

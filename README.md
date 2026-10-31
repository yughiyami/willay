# Willay Relaves

An El Nino early-warning agent for Peru. It watches tailings sites (relaves) and the communities
downstream of them, and raises alerts on three horizons: weeks to months, 1 to 3 days, and hours.

Built for the Meta Global AI Developer Hackathon.

> **Honesty notes**
>
> - Willay Relaves **complements** the official SENAMHI, INDECI and ENFEN alerts. It **never replaces** them.
>   Every outgoing message says so and cites its official source.
> - **The LLM never decides an alert level.** Tiers come from deterministic rules in
>   `backend/willay/domain/tiers.py`. Language models may only rephrase or explain, and a rephrased
>   message is rejected (the template is used instead) if any required fact is missing.
> - **EVACUATION needs a human.** Without explicit operator confirmation the result is
>   `PENDING_CONFIRMATION`, never `EVACUATION`.
> - **Two data modes.** `WILLAY_DATA_MODE=real` (default) reads open datasets from `WILLAY_DATA_DIR`
>   (default `../willay-data`, never copied into this repo). `WILLAY_DATA_MODE=synthetic` uses the invented
>   fixtures in `backend/fixtures` (flagged `synthetic: true`; the test suite uses this mode).
> - **Real data is sparse and approximate**: tailings risk/status are blank for most sites (flagged
>   `data_quality`, neutral defaults), water intakes are approximate OpenStreetMap locations, flow paths come
>   from a 30 m DSM. See `docs/BACKTEST.md` for what the backtest can and cannot show.

## Alert tiers

| Tier | Horizon | Rule |
|------|---------|------|
| `PREPARATION` | weeks to months | ENFEN coastal El Nino magnitude >= strong AND seasonal rainfall anomaly forecast `above_normal` AND site static risk >= threshold |
| `ATTENTION` | 1 to 3 days | SENAMHI warning for the province is orange or red, OR forecast rain next 72 h >= threshold |
| `PENDING_CONFIRMATION` | hours | Evacuation conditions hold but no operator confirmation yet (operator-facing only) |
| `EVACUATION` | hours | Observed rain >= 20 mm in 24 h AND (measured water rise >= threshold from video segmentation OR a community watcher report) AND explicit operator confirmation |

ENFEN scale: neutral < weak < moderate < strong < extraordinary. All thresholds are configurable (`TierConfig`).

## Risk score

`risk = Trigger(t) x Susceptibility x TailingsVulnerability x DownstreamExposure`, each factor in [0, 1].

- A small configurable epsilon floor keeps a missing or zero factor from silently zeroing the score.
  The API returns the product and a per-factor breakdown (`raw`, `value`, `floored`, `missing`).
- **TailingsVulnerability**: log-normalized volume, status (abandoned is worst), declared risk level,
  distance to river (closer is worse).
- **DownstreamExposure**: settlements snapped to the downstream flow path (tolerance 500 m, haversine, no GIS
  libraries), weighted by `exp(-d / L)` (default L = 10 km), cutoff 50 km; population, households and drinking-water
  intakes raise the impact; impacts combine as a noisy-OR so the result saturates at 1.

## Real-data pipeline

Offline scripts (need numpy, shapely, tifffile from `requirements.txt`) write only to `willay-data/derived/`:

```bash
.venv/Scripts/python -I scripts/build_flowpaths.py          # D8 paths from the Copernicus DEM (flowpaths.geojson)
.venv/Scripts/python -I scripts/validate_flowpaths.py       # checks paths against IGN rivers
.venv/Scripts/python -I scripts/build_site_features.py      # susceptibility sample + distance to river
.venv/Scripts/python -I scripts/build_district_features.py  # static district features + simplified polygons
.venv/Scripts/python -I scripts/run_backtest.py             # derived/backtest_results.json
```

Unknown tailings status/risk map to `unknown` and use documented neutral defaults (0.7 / 0.5) plus a
`data_quality` flag. ANA population is `Pob_rev` (never the corrupted `Pob`). The CENEPRED 2027 scenario is
only a comparison baseline.

## Alert languages

Household messages default to neutral professional Spanish (`es`); `en` is kept (operator messages default
to `en`). Other languages such as Quechua are **never machine-generated**: they need a human-reviewed template
file `backend/templates/reviewed/<lang>.json` (`WILLAY_REVIEWED_TEMPLATES_DIR` overrides the folder; format in
`willay/adapters/reviewed_templates.py`). Without it `/alerts/evaluate` returns the tier and
`message.available: false` with the reason.

## Backtest metrics

Pure functions with tests, no sklearn: ROC AUC (Mann-Whitney, ties count one half), top-k% capture rate,
lead time between the first PREPARATION alert and the emergency (median, p25, p75), and the false-alarm rate of
EVACUATION alerts (share of alerts not followed by an emergency).

## Architecture

Hexagonal, screaming structure. Dependencies point inward only (enforced by `test_architecture.py`).

```
backend/
  willay/
    domain/        entities, risk.py, tiers.py, exposure.py, backtest_metrics.py, geo.py (pure Python)
    application/   rank_sites, evaluate_alert_tier, compose_alert_message, run_backtest
    ports/         Protocols: SiteRepository, HazardData, Weather, Messaging, LLM, Segmentation, Image, Transcription
    adapters/      in-memory fakes, fixture loader, meta_model_api (OpenAI-compatible HTTP skeleton)
    api/           FastAPI app
  fixtures/        synthetic Rimac-basin and backtest JSON
  tests/           unit + api
web/               Vite + CesiumJS globe
```

## Run the backend

Requires Python 3.13.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt     # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # macOS/Linux

.venv/Scripts/python -m pytest                                   # run all tests (synthetic mode)

cd backend
../.venv/Scripts/python -m uvicorn willay.api.app:app --reload --port 8000
```

Endpoints (interactive docs at `http://localhost:8000/docs`):

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | liveness, `synthetic_data` flag |
| GET | `/sites?basin_only=&detail=` | basin sites ranked by risk: factor breakdown, `data_quality`, reached settlements/intakes |
| GET | `/sites/{id}` | one site: breakdown, reached settlements (name, population, distance_km), intakes, flow path GeoJSON, static inputs, current tier |
| GET | `/flowpaths` | all flow paths (GeoJSON, cumulative distance per vertex) |
| GET | `/districts` | district GeoJSON: static score, CENEPRED baseline, INDECI emergencies 2017/2023 |
| GET | `/layers/pasivos`, `/layers/intakes` | MINEM 2011 liabilities (secondary), approximate OSM intakes |
| POST | `/alerts/evaluate` | hazard inputs (+ `site_id` or `site_static_risk`, optional `language`) to tier, reasons and message |
| GET/POST | `/backtest?event=2017\|2023` | stored district backtest (synthetic mode: the synthetic fixture) |

Example:

```bash
curl -X POST localhost:8000/alerts/evaluate -H "Content-Type: application/json" \
  -d '{"site_static_risk": 0.9, "observed_rain_24h_mm": 30, "water_rise_cm": 45}'
# -> "tier": "PENDING_CONFIRMATION"; add "operator_confirmed": true for EVACUATION
```

## Run the web app

Requires Node 20+. Start the backend first (port 8000).

```bash
cd web
npm install
npm run dev      # http://localhost:5173
npm run build    # type-check and production build
```

The globe is centered on the Rimac basin, uses OpenStreetMap imagery and an ellipsoid terrain (no Cesium ion
token needed). Points are colored by risk; selecting one shows the factor breakdown, current tier and
downstream settlements. Set `VITE_API_BASE_URL` to point at another backend.

## Meta model API (optional)

Copy `.env.example` to `.env` (never commit it) and set `META_API_KEY`. The adapter in
`backend/willay/adapters/meta_model_api.py` talks to the OpenAI-compatible base URL
`https://api.meta.ai/v1` with `muse-spark-1.3` (text), `muse-voice-transcribe-1.0` (transcription),
`sam-3.1` (segmentation, still a skeleton) and `muse-image-1.0` (images). Its tests use a mocked HTTP
transport only; the test suite never calls the real API.

## Status

Real-data adapters for the INGEMMET tailings inventory, ANA settlements, OSM intakes, INDECI, CHIRPS and ENFEN
timeline are in place behind the existing ports. Not yet connected: live SENAMHI warnings and forecasts,
real LLM/segmentation calls, SEDAPAL intake locations.

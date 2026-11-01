# Public signals (crowd / news corroboration)

## Purpose

Willay Relaves decides alert tiers from deterministic rules on official inputs (ENFEN, SENAMHI, rain, a measured water rise).
**Public signals** add one more thing for the operator to look at: reports on the open web (news and public pages, last 72 hours)
that a huaico, landslide, overflow, tailings spill or flood happened in a place of the Rimac basin.

They are labelled **"señales públicas no verificadas"** everywhere (API, tool results, globe layer, Evidencia tab) and they are
**context only**:

| Can | Cannot |
|---|---|
| Add one reason to an `ATTENTION` evaluation: `corroborated by N public signals` (N >= 2 distinct pages in the site's district or downstream) | Create, raise or lower any tier |
| Be listed per district with links, shown on the globe, read by the HazardAnalyst through `get_public_signals` | Reach `PENDING_CONFIRMATION` or `EVACUATION` |
| | Start, propose or approve a dispatch, or touch the household ledger |

The guard is in the domain (`willay/domain/signals.py::corroborate`): it takes a `TierEvaluation` and returns one with the same
`tier` and `triggered` for every input, and only an evaluation that is already `ATTENTION` gets the extra reason. It is tested
for every tier and for signal counts up to 10 000 (`backend/tests/unit/test_signals_domain.py`), through the orchestrator
(`test_orchestrator_signals.py`: no proposal from signals alone, PENDING_CONFIRMATION identical with and without signals) and over
HTTP (`tests/api/test_api_signals.py`).

## Pipeline

1. **Search** (`adapters/tavily_search.py`, port `PublicSearchPort`). For groups of the districts that hold our ANA settlements,
   one query ORs the five hazard terms (huaico, deslizamiento, desborde, relave, inundación) with up to five district names. Each
   query makes two Tavily calls: `topic=news` with `days=3` and `topic=general` with `start_date` = 72 h ago. Social platforms are
   excluded in the request (`exclude_domains`) and filtered again on arrival. Only **title, URL, published date and a snippet
   of at most 400 characters** are kept from each result; raw page content, authors and images are neither requested nor stored.
2. **Extraction** (`application/public_signals.py`, existing `StructuredExtractionPort`, Muse Spark `muse-spark-1.3`, structured
   output). The model sees only those four fields and returns `{event_type, place_name, district_ubigeo_guess, event_time,
   confidence, source_url}` per reported event. In code, an event is dropped when the type is unknown, the confidence is outside
   [0.5, 1], the place is empty, the time is outside the 72 h window (a missing or garbled time falls back to the page's
   publication date), or `source_url` is not one of the URLs it was given (the model cannot invent a link). Extra fields the model
   adds are never carried.
3. **Geocoding** (`domain/signals.py::Gazetteer`). `place_name` is matched, **exactly after normalization** (case, accents,
   punctuation, spacing), against our own settlement names (ANA) and district names. No fuzzy matching. No match: the event is
   dropped (and counted in the refresh report). The same name in several districts needs `district_ubigeo_guess` to select exactly
   one; the guess never creates a match by itself. Several settlements with one name inside one district resolve to the district.
   The stored place is **our** canonical name, never the text of the page.
4. **Aggregation** (`aggregate_by_district`). Per district: number of distinct pages, latest event time and the source links; a page
   counts once per district.
5. **Discard**. Titles, snippets and model answers live only inside one refresh call. The hub keeps, per signal: event type, our
   place name, district, coordinates, event time, extractor confidence, and the page URL and domain.

## Data minimization

- Stored: event type, canonical place, district/UBIGEO, coordinates of that place, event time, confidence, **domain and URL of the
  source page**. URLs are cleaned: credentials, fragments and tracking parameters (`utm_*`, `fbclid`, `gclid`, ...) are removed.
- Never stored: article text, titles, snippets, model output, **author names, social handles, profile URLs, phone numbers or any
  other personal data**. Social-platform domains are never searched, sent to the model or kept; a test asserts that titles and
  snippets do not appear in the hub state or in `GET /signals`.
- State is in memory only (lost on restart); nothing is written to disk by this feature.
- What leaves the machine: the query text (hazard terms and district names) to Tavily; the four public fields of each result to the
  Meta Model API for extraction (`WILLAY_SPECIALIST_MODEL` is not used here; extraction uses `muse-spark-1.3`). Meta's
  free-credit terms allow content to be used by Meta (see `docs/META_API.md`): only public page fields are sent, never households
  or operator data.
- Cost control: at most `WILLAY_SIGNALS_MAX_QUERIES` queries (default 8, two Tavily calls each), results in batches of 10 per model
  call, a cooldown between refreshes (default 300 s), and the Meta usage log and `WILLAY_META_BUDGET_USD` still apply.

## Legal notes (to be verified)

- **Ley N.° 29733, Ley de Protección de Datos Personales (Peru)** and its regulations: signals are built to hold no personal data,
  but a news page can still name people and the model reads a snippet of it. Whether reading public pages this way, and sending
  snippets to a foreign model provider (cross-border flow), needs a legal review before any operational use. **Not verified by
  counsel; do not treat this note as legal advice.**
- **Terms of service**: Tavily is queried through its documented Search API, which searches the public web. Willay does **not**
  scrape social platforms (Facebook, X/Twitter, Instagram, TikTok, YouTube, Telegram, Reddit, ...): they are excluded from the
  search and dropped if they show up. Publishers' own terms apply to the pages the links point to; Willay stores links, not
  content. Confirm Tavily's current terms for your use before relying on it.
- **Reliability**: these are unverified reports that can be wrong, stale, duplicated or about another place with the same name.
  They complement, and never replace, the official SENAMHI, INDECI and ENFEN alerts.

## How to enable

Environment variable NAMES (values go in your local `.env`, never in the repository; `.env.example` is write-protected for the
tooling that built this, so add the names there yourself):

```
WILLAY_SIGNALS_ENABLED=0        # default 0. Set 1 to turn on live signals
TAVILY_API_KEY=                 # your Tavily key
WILLAY_SIGNALS_MAX_QUERIES=8    # optional: queries per refresh
WILLAY_SIGNALS_COOLDOWN_S=300   # optional: minimum seconds between refreshes
TAVILY_API_BASE_URL=            # optional override of https://api.tavily.com
```

Live signals also need the extraction model: `WILLAY_META_ENABLED=1` and `META_API_KEY` (see `README.md`). A key alone never enables
anything, and a missing piece leaves the app running with signals off and a note that names the missing variable (never a value).

1. Start the backend with the variables above.
2. `POST /signals/refresh` (or the "Actualizar (búsqueda en vivo)" button in **Evidencia**) searches, extracts, geocodes and stores.
   A second call within the cooldown answers 429.
3. `GET /signals?district=<name or ubigeo>` returns `{mode, label, disclaimer, window_hours, refreshed_at, districts: [{ubigeo,
   district, count, latest, lon, lat, sources: [{url, domain, event_type, event_time}]}], signals: [...]}`.
4. The HazardAnalyst can call `get_public_signals(district | site_id)`; `evaluate_alert`, `prepare_dispatch` and `POST /alerts/evaluate`
   (with `site_id`) add the corroboration reason on `ATTENTION`.

### Demo mode (simulated)

Without any key, `backend/fixtures/signals/public_signals_simulated.json` holds 7 **synthetic** signals (all links
`https://example.org/simulado/...`, times relative to "now", flagged `"synthetic": true`; the loader refuses a file that is not
flagged or whose links are not on example.org). Starting **Modo demostración** in the console, or pressing "Mostrar señales
simuladas (demostración)" in Evidencia, shows them on the globe and in the list marked **simulado**
(`POST /signals/simulated {"enabled": true}`). The simulated set is never mixed with live signals (409 while live is enabled).

## What is not verified

- The Tavily request shape follows its public documentation (`topic`, `days`, `start_date`, `exclude_domains`, Bearer
  authorization) and is covered by mocked-HTTP tests only. **No live Tavily or Meta call has been made for this feature.** Run one
  refresh on purpose before trusting it, and check the actual field names (`published_date` format, `content`).
- Extraction quality (false positives, missed events, place-name variants) is unmeasured. Exact geocoding trades recall for
  precision: many real reports will be dropped because the page says "Chosica" and we hold "LURIGANCHO", or the reverse.
- The queries cover only the first `WILLAY_SIGNALS_MAX_QUERIES` groups of district names (alphabetical).

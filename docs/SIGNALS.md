# Public signals (crowd / news corroboration)

> **WARNING: social-media search through TikHub (optional, OFF by default).** `WILLAY_TIKHUB_ENABLED=1` makes Willay read public
> posts of TikTok and X/Twitter through TikHub, a third-party scraping API. **The platforms' terms of service may prohibit scraping
> or automated collection, TikHub's own terms apply, and Peru's Ley N.° 29733 (personal data protection) applies to anything that
> can identify a person.** None of this has been reviewed by counsel. The operator must verify the legality for their own use, and
> obtain any required authorisation, before enabling it. It is disabled unless the operator sets the flag; a key alone never enables
> it. Without the flag Willay searches only news and public web pages (Tavily) and never touches a social platform.

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
   excluded in the request (`exclude_domains`) and filtered again on arrival. With `WILLAY_TIKHUB_ENABLED=1` a second source,
   `adapters/tikhub_search.py`, adds public TikTok and X posts (see "TikHub social posts" below). Only **title, URL, published date and a snippet
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
  other personal data**. Unless TikHub is enabled, social-platform domains are never searched, sent to the model or kept; a test
  asserts that titles and snippets do not appear in the hub state or in `GET /signals`.
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
- **Terms of service**: Tavily is queried through its documented Search API, which searches the public web. The Tavily path does **not**
  scrape social platforms (Facebook, X/Twitter, Instagram, TikTok, YouTube, Telegram, Reddit, ...): they are excluded from the
  search and dropped if they show up. The optional TikHub source is the one exception, only for TikTok and X, only when the
  operator turns it on, and subject to the warning at the top of this page. Publishers' own terms apply to the pages the links point to; Willay stores links, not
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

## TikHub social posts (optional, off by default)

**Read the warning at the top of this page first.** Platform terms may prohibit collecting their content with third-party scrapers;
the operator is responsible for checking that, for TikHub's terms and for Ley 29733 before enabling this source.

What it does (`adapters/tikhub_search.py`, same `PublicSearchPort` as Tavily, queries built by the same use case):

| Platform | Endpoint (GET, `Authorization: Bearer <TIKHUB_API_KEY>`, base `https://api.tikhub.io`) | Parameters used |
|---|---|---|
| TikTok | `/api/v1/tiktok/app/v3/fetch_video_search_result` | `keyword`, `offset=0`, `count`, `sort_type=0`, `publish_time=7` (last week; the 72 h window is applied in code), `region=PE` |
| X / Twitter | `/api/v1/twitter/web/fetch_search_timeline` | `keyword` (one boolean query), `search_type=Latest` |

Paths and parameter names come from TikHub's OpenAPI document (`https://api.tikhub.io/openapi.json`). Keywords are the same hazard
terms (huaico, deslizamiento, desborde, relave, inundación) and Rímac-basin place names as the web search. TikTok search has no OR, so
the adapter pairs a hazard term with a place in turn (`huaico Chosica`, `deslizamiento Ñaña`, ...), at most
`WILLAY_TIKHUB_MAX_REQUESTS` (default 5) requests per query; X gets one boolean query. Each request is billed by TikHub.

Data minimization, identical to the Tavily path and stricter in two places:

- Per post, only the **canonical post URL, the platform, the publish time and a snippet of at most 400 characters** are read; the
  snippet is used solely for extraction and then discarded with the rest of the refresh state.
- The URL is rebuilt from the post id (`https://www.tiktok.com/@/video/<id>`, `https://x.com/i/status/<id>`), so it carries **no
  handle**. Usernames, handles, display names, profile URLs, avatars, follower counts and every other field are never copied into a
  result (a test asserts none of them survives).
- Before the snippet leaves the adapter, `@mentions`, links, e-mail addresses and phone-number-like digit runs are removed (hashtags stay).
- **Images and video are never requested, downloaded or processed** (the adapter calls only the two search endpoints and reads only
  text), so posts showing faces or people are never analysed. Nothing is written to disk.
- Same guardrail as every signal: a social post counts as one distinct source, at least **2 distinct sources** are needed for the
  "corroborated by N public signals" reason, and that reason can only be added to an evaluation that is **already `ATTENTION`**. Signals
  never raise a tier, never reach `PENDING_CONFIRMATION` or `EVACUATION`, never start a dispatch.
- A social URL is accepted only when it comes from this adapter (`SearchResult.platform` is set and matches the host: `tiktok.com`, `x.com`,
  `twitter.com`). Any other social URL, and any social URL from another source, is still dropped.

Enabling it needs all of: `WILLAY_SIGNALS_ENABLED=1`, `WILLAY_TIKHUB_ENABLED=1`, `TIKHUB_API_KEY`, and the extraction model
(`WILLAY_META_ENABLED=1`). Tavily and TikHub can run together or alone (a missing key for one leaves the other running, with a note
naming the missing variable). The posts' text is read by Muse Spark, a foreign model provider, so the cross-border note above applies
to social snippets too.

```
WILLAY_TIKHUB_ENABLED=0         # default 0. Set 1 only after verifying platform terms, TikHub terms and Ley 29733
TIKHUB_API_KEY=                 # your TikHub token (sent only in the Authorization header)
WILLAY_TIKHUB_PLATFORMS=tiktok,twitter   # optional: subset of tiktok,twitter
WILLAY_TIKHUB_MAX_REQUESTS=5    # optional: TikTok requests per query (each is billed)
TIKHUB_API_BASE_URL=            # optional override of https://api.tikhub.io
```

**Not verified**: TikHub's OpenAPI describes responses only as a generic envelope (`code`, `message`, `data`), so the post parsers
look tolerantly for TikTok objects with `aweme_id`, `desc`, `create_time` and X objects with `rest_id`/`legacy.full_text`/
`legacy.created_at` (or flat `tweet_id`/`text`/`created_at`). The adapter is covered by mocked-HTTP tests only; **no live TikHub call
has ever been made**. Make one request on purpose and compare the real field names before trusting it. Search quality (X's search
does not guarantee that the keyword is in the tweet text) is unmeasured.

## What is not verified

- The Tavily request shape follows its public documentation (`topic`, `days`, `start_date`, `exclude_domains`, Bearer
  authorization) and is covered by mocked-HTTP tests only. **No live Tavily or Meta call has been made for this feature.** Run one
  refresh on purpose before trusting it, and check the actual field names (`published_date` format, `content`).
- Extraction quality (false positives, missed events, place-name variants) is unmeasured. Exact geocoding trades recall for
  precision: many real reports will be dropped because the page says "Chosica" and we hold "LURIGANCHO", or the reverse.
- The queries cover only the first `WILLAY_SIGNALS_MAX_QUERIES` groups of district names (alphabetical).

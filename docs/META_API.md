# Meta Model API: verified contract and how Willay uses it

Checked on 2026-10-31 against the official docs (rendered pages read with a browser; the summarising fetch tool
dropped most request details). Items marked **live** were also exercised once against the real API with the smoke
tests in `backend/tests/live/` (`pytest -m live`). Prices are the published pay-as-you-go prices; the "est. cost"
column in `derived/meta_usage.jsonl` is computed from them, not read from a billing dashboard.

Base URL `https://api.meta.ai/v1`. Auth: `Authorization: Bearer <key>` (the docs call the variable `MODEL_API_KEY`;
this project reads `META_API_KEY` and optional `META_API_BASE_URL` from `.env`, never printed or committed).

## Corrections to the old skeleton

| Capability | Old adapter | Verified contract |
|---|---|---|
| Text | `POST /chat/completions` | `POST /responses` (recommended; Chat Completions also exists) |
| Speech to text | `POST /audio/transcriptions`, JSON `text` | `POST /asr/transcribe`, multipart `request` + `audio`, response `transcript` |
| Image | `POST /images/generations` with `response_format` | same path; `output_format` `png`, `n`, `size` (aspect ratio); `data[].b64_json` |
| SAM | `NotImplementedError` | `POST /responses` with `model: sam-3.1`, `input_video`/`input_image`, streamed special-token text |

## Models

| Use | Model | Notes |
|---|---|---|
| Agent, extraction, rephrase (no audio) | `muse-spark-1.3` | 1.3 audio input is "not fully supported": send audio to `muse-voice-transcribe-1.0`, or use `muse-spark-1.2` if audio must go to Spark |
| Judge (advisory) | `muse-wonder-1.0` | preview, Standard mode only |
| Speech to text | `muse-voice-transcribe-1.0` | WAV only |
| Images | `muse-image-1.0` | |
| Segmentation | `sam-3.1` | images and video |

Standard-mode models do not train on content; `*-contributor` variants (discounted) do. **We only use Standard-mode
models.** Even so, never send personal data: only public official documents, synthetic households and clips without people.

## Responses API: text, structured output, tool calling (**live**)

- Request: `model`, `input` (string or items), `instructions` (not carried across `previous_response_id`: resend it every turn),
  `tools`, `previous_response_id`, `text.format`, `reasoning.effort` (`minimal|low|medium|high|xhigh`; `none` is rejected),
  `max_output_tokens` (min 16, counts reasoning), `store` (default true; required for `previous_response_id`).
- Output text: `output[]` items with `type: "message"` -> `content[].type: "output_text"` -> `text`.
- Usage: `usage.input_tokens`, `output_tokens`, `input_tokens_details.cached_tokens`, `output_tokens_details.reasoning_tokens`.
  Spark reasons by default, so even tiny answers use hundreds of output tokens (live: a 110-token rephrase produced 670).
- **Structured output** (Responses): `text: {"format": {"type": "json_schema", "name": ..., "schema": ..., "strict": false}}`.
  Decoding is always schema-constrained; `strict: true` only validates that the schema is in the strict subset
  (`additionalProperties: false`, all keys required). Limits: depth 10, 5,000 properties, no recursive schemas.
  Chat Completions uses `response_format` instead (a parameter from the other endpoint does nothing).
- **PDF input**: `{"type": "input_file", "filename": ..., "file_data": "data:application/pdf;base64,..."}` (50 MB inline;
  text from the first 100 pages, images from the first 50). Live: ENFEN N°18 = 7,459 input tokens.
- **Function calling**: flat tool definitions `{"type":"function","name","description","parameters"}`. The model returns
  `function_call` output items (`call_id`, `name`, JSON-string `arguments`). Reply with `previous_response_id` and
  `{"type":"function_call_output","call_id","output"}` items. Only `tool_choice: "auto"` is supported; always validate arguments.
  `parallel_tool_calls` defaults to true. Live: a 3-turn tool loop worked and reported `cached_tokens`.
- Price (Standard): input $1.25 / cached $0.15 / output $4.25 per 1M tokens (Wonder: $4 / $0.20 / $20).

## Speech to text (**live**)

- `POST /asr/transcribe` (optional `?sessionId=`), `multipart/form-data`: part `request` (JSON) and part `audio`.
  `request`: `{"model":"muse-voice-transcribe-1.0","audioEncoding":"WAV","mode":"PUSH_TO_TALK","languageBias":["Spanish"]}`;
  optional `keywords`. `Accept: application/json` returns `{sessionId, transcript, audioDurationMs, turns[]}`.
- Audio: **WAV only**, mono 16-bit PCM at 16 or 24 kHz, <= 10 min, <= 32 MB. The web app records with MediaRecorder
  (webm/opus), decodes and resamples to 16 kHz mono WAV in the browser (`web/src/audio.ts`).
- Price $0.18 per hour of audio, whole seconds. 429 means back off. Live: a 4.2 s Spanish clip (Windows "Sabina" TTS voice,
  so no person's voice) transcribed correctly.

## Muse Image (**live**)

- `POST /images/generations`: `{"model":"muse-image-1.0","prompt","n":1,"output_format":"png","response_format":"b64_json","size":"1024x1536"}`.
  `size` sets the aspect ratio only. Response: `data[].b64_json`, `usage` (token counts for reference only).
  $0.01 per generated image regardless of tokens or search. Also reachable through `/responses` (multi-turn editing).
- Live: the Spanish evacuation card came back as a 1280x1920 PNG with the text exactly as requested
  (`derived/cards/live_smoke_evacuation.png`). Image models can misrender text in general, so cards stay `review_required`.

## SAM 3.1 (**live**: contract; not validated on real rivers)

- Endpoint: `POST /responses`, `model: "sam-3.1"`, one user message with an `input_text` part and either
  `{"type":"input_image","image_url"}` or `{"type":"input_video","video_url"|"file_id"}` (public URL or base64 data URL;
  `POST /files` with `purpose=user_data` for clips reused). Use `stream: true` (recommended; the response is SSE:
  `response.output_text.delta` per frame, `response.completed` carries `usage.video_frames_processed`).
- **One noun phrase per request.** Commas/"and" lists return nothing; commands and questions are unreliable. To segment water
  and a marker we send two requests ("water", "red post" by default, configurable).
- Box/point prompts (not used by Willay yet): `<30f>1<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|>`; `x2`,`y2` inclusive; negative
  points prefixed with `-`; up to 16 objects; `metadata.propagation_direction` `both|forward|backward`.
- `metadata` (strings): `mask_encoding` `lossless` (default) or `one_bit` (smaller; we use it), `include_confidence` `"true"` adds `c`.
- Output (one line per frame, frames with no match are absent and may be out of order for box prompts):
  `<Nf>id<|box;x1;y1;x2;y2;w;h;c|><|mask;x=0;y=0;c;data=H,W,<~|!>base85|>,id...`. Ids are stable per object across frames.
  The mask raster `H x W` is **box-local** (it covers the box, not the frame). The mask payload encoding is not specified on the
  docs pages, only through the official parser `meta-sam-parser` (zero-dependency Python, pinned in `requirements.txt`), which
  we use behind `MetaModelApiAdapter.segment_video` and convert to our stdlib bit-row `Mask`. Zero matches = an empty stream
  that completes normally.
- Limits: MP4 or MOV, H.264/H.265/MPEG-4 (no VP9/WebM); up to 15,000 frames per request; 16 tracked objects; the server remuxes
  MP4s without faststart losslessly.
- Billing: **$0.20 per 1,000 video frames, minimum 50 frames per request**; $2.50 per 1,000 images. `usage.video_frames_processed`
  reports billed frames. Our river watcher makes two requests per clip.
- Live: one request, prompt "water", on the 60-frame synthetic clip -> contract confirmed (SSE, parser, masks, billed 60 frames,
  $0.012). SAM returned a water match on 32 of 60 frames of that flat-coloured fake scene, which shows nothing about real water
  (see `docs/WATCHER_PROTOCOL.md`).

## Multi-agent orchestration: what the live smoke showed (2026-10-31)

One recorded run (`backend/fixtures/demo/live_trace.json`, about US$0.15) exercised the planner and four specialists end to end.

- `muse-wonder-1.0` **accepts flat function tools and `previous_response_id` chaining** on `/responses`, exactly like Spark (tool calls,
  `function_call_output`, usage fields). A tool-calling turn cost about US$0.013-0.025.
- **Transient server errors do occur**: `HTTP 503 persistence service error` and `HTTP 400 referenced response not found or expired`
  (a `previous_response_id` created a moment earlier). The adapter retries 429/5xx and that 400 twice with backoff; if a planner turn still
  fails, the orchestrator falls back to `muse-spark-1.3` for the rest of the run and says so in the trace.
- Spark reasoning counts against `max_output_tokens`: 1,400 returned an empty answer once, so the agents use 3,000.
- SAM prompt wording matters on the fake scene: `water` found the river in 35 of 60 frames (rise 29.2 cm vs 45 true), `river` in none,
  `blue water` in 60 of 60 (45.0 cm, confidence 0.78). One synthetic clip, not a benchmark.

## Rate limits

Per team and model: Spark 3,000 RPM / 4M TPM (Contributor variants 150 RPM), SAM 3,000 RPM, Muse Image 150 RPM, Voice
256 concurrent streams and 16,000 streams per hour. Retry 429 with exponential backoff and jitter.

## What this project enforces on top

- Every live call appends metadata (model, endpoint, purpose, tokens/frames/images/seconds, est. cost) to
  `willay-data/derived/meta_usage.jsonl`; prompts, media and headers are never stored.
- A spend guard refuses a call whose estimated cost would take the cumulative total over `WILLAY_META_BUDGET_USD` (default 3.0).
- The key is redacted from every error message. Nothing is wired unless `WILLAY_META_ENABLED=1`.
- The model never decides an alert tier: tiers come from `domain/tiers.py`; the agent's tools return them and a guard replaces
  an answer that states a tier no tool returned.

## Not verifiable from the docs or not exercised (explicit list)

1. Whether the model's masks and tracking are good enough on **real river video** with a painted post, and the best prompt
   wording for the marker ("red post", "measuring stick"). Only the contract was checked, on a fake clip.
2. `video/quicktime` as the MIME type for MOV data URLs, and the Files API upload path for clips larger than the inline limit.
3. The exact SSE error event shape (`response.failed` / `error`) was taken from the docs and unit-tested with mocks, never triggered live.
4. `reasoning.effort: "low"` is accepted live, but the best effort level per task was not tuned.
5. `strict: true` structured output and `tool_choice` other than `auto` (documented as unsupported).
6. Billing accuracy: costs are estimates from published prices; the dashboard was not consulted. Spark reasoning tokens
   make text calls cost more than their visible output suggests.
7. Realtime ASR (WebSocket) was not used; push-to-talk file transcription only.
8. ENFEN communique N°19 (30 Oct 2026) could not be located or verified, so it was not ingested.

## Sources

- https://dev.meta.ai/docs (index), /docs/models, /docs/pricing-rate-limits
- https://dev.meta.ai/docs/protocols/responses, /docs/api-reference/responses/schemas
- https://dev.meta.ai/docs/tool-calling, /docs/structured-output, /docs/file-handling
- https://dev.meta.ai/docs/speech-to-text, /docs/api-reference/voice/transcribe
- https://dev.meta.ai/docs/image-generation, /docs/api-reference/images/create-image
- https://dev.meta.ai/docs/media-segmentation, /docs/sam/segmenting, /docs/sam/reading-segmentation, /docs/sam/client-libraries

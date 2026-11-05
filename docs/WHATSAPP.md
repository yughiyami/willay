# WhatsApp: household alerts and replies through the Meta WhatsApp Business Cloud API

Willay Relaves can message a household on WhatsApp with the alert text plus two reply buttons (**Estoy a salvo** /
**Necesito ayuda**), and handle what the household sends back. It uses the official **WhatsApp Business Cloud API**
(Graph API). The web phone mock-up keeps working: every message really sent is mirrored into it.

> **Safety first**
>
> - **Simulated by default.** WhatsApp is used only when `WILLAY_MESSAGING_PROVIDER=whatsapp` **and** the four
>   `WHATSAPP_*` secrets below are set. Otherwise the web simulator stays active and `GET /health` reports
>   `messaging_mode` (for example `simulated (WhatsApp selected but not configured: missing environment variables: ...)`,
>   names only, never values).
> - **Allow-list.** The adapter refuses (before any request) numbers outside `WILLAY_MESSAGE_ALLOWLIST`, or
>   `WILLAY_CALL_ALLOWLIST` when the former is unset. An empty list refuses everything. Inbound messages from numbers
>   outside the list are ignored silently, so a stranger cannot spend model budget.
> - Dispatch only contacts the **synthetic demo households**. To receive a real message, copy
>   `backend/fixtures/households_synthetic.json` to a local file, put your own number in it, point
>   `WILLAY_DEMO_HOUSEHOLDS_FILE` at it and allow-list the same number.
> - The token, app secret and verify token are read from the environment only. They never appear in a URL, log line,
>   API response or `repr`.

## Set up in Meta (about 15 minutes)

1. Go to <https://developers.facebook.com/apps> and **Create app**; choose use case **Other**, app type **Business**.
2. In the app dashboard, **Add product > WhatsApp** (Cloud API). Meta creates a **test number** and a test WhatsApp
   Business Account for you.
3. **API Setup** page: under *To*, **add the recipient phone number** you will test with (up to 5) and confirm the code
   WhatsApp sends to it. Copy the **Phone number ID** (this is `WHATSAPP_PHONE_NUMBER_ID`, not the phone number).
4. Token: the **temporary access token** on that page expires in about 24 hours. For anything longer, create a
   **System User** in Business Settings > Users > System users, assign it the app and the WhatsApp account, and generate
   a token with `whatsapp_business_messaging` and `whatsapp_business_management` (this is `WHATSAPP_TOKEN`).
5. **App settings > Basic > App secret** > Show: this is `WHATSAPP_APP_SECRET` (it signs every webhook).
6. Expose the backend over HTTPS (for example a Cloudflare Tunnel, as for Twilio) and set `WILLAY_PUBLIC_BASE_URL`.
7. **WhatsApp > Configuration > Webhook > Edit**: callback URL `<WILLAY_PUBLIC_BASE_URL>/whatsapp/webhook`, verify token =
   the value you chose for `WHATSAPP_VERIFY_TOKEN` (any random string). Start the backend **first**: Meta calls
   `GET /whatsapp/webhook` while you click *Verify and save*.
8. Under *Webhook fields*, **subscribe to `messages`**.
9. **Open the 24 h window** (see below): from the recipient phone, send "hola" to the test number.
10. Check the link: `curl -X POST <base>/whatsapp/test -H 'Content-Type: application/json' -d '{"to":"+51..."}'` sends a
    Spanish hello with the two buttons; `GET /whatsapp/log` lists what happened.

## Environment variables (names only; put values in your local `.env`, never in git)

| Variable | Default | Purpose |
|---|---|---|
| `WILLAY_MESSAGING_PROVIDER` | `simulated` | `whatsapp` enables the Cloud API (still simulated if the rest is missing). Export it in the shell that starts the backend; the secrets below are read from `.env` |
| `WHATSAPP_TOKEN` | none | access token (System User token recommended) |
| `WHATSAPP_PHONE_NUMBER_ID` | none | numeric id of the sending phone number |
| `WHATSAPP_VERIFY_TOKEN` | none | string you choose; Meta echoes it on the webhook handshake |
| `WHATSAPP_APP_SECRET` | none | app secret; validates `X-Hub-Signature-256` |
| `WHATSAPP_GRAPH_VERSION` | `v26.0` | Graph API version (`v25.0` or `25.0` are accepted) |
| `WILLAY_MESSAGE_ALLOWLIST` | falls back to `WILLAY_CALL_ALLOWLIST` | comma-separated E.164 numbers that may receive or be answered |
| `WILLAY_PUBLIC_BASE_URL` | none | public https base URL of the backend (used only to build the webhook URL you paste into Meta) |
| `WILLAY_META_ENABLED` / `META_API_KEY` | off | needed for voice notes (Voice Transcribe) and model answers (Muse Spark) |

## The 24 h window (read this before the demo)

WhatsApp only lets a business send free-form text or buttons within **24 hours after the user's last message**. The
first time, **the user must message the bot first** (for example "hola"). Outside the window Meta rejects the send with
error **131047**. Willay then falls back to the pre-approved `hello_world` (en_US) template, which every test number
has, and says so honestly: the alert gets `message_status = template_only` with a note, and `GET /whatsapp/log` shows
the fallback. **That template does not contain the alert text.** Error **131030** means the recipient is not in the test
number's recipient list (step 3).

## What the bot does with each inbound message

| Message | Behaviour |
|---|---|
| Button **Estoy a salvo** / **Necesito ayuda** (`SAFE:<alert_id>` / `HELP:<alert_id>`) | records `SAFE` / `NEEDS_HELP` with channel `whatsapp`; `NEEDS_HELP` opens an operator case. Replies "Gracias, registramos que está a salvo." / "Un operador fue notificado. Si puede, comparta su ubicación." The sender must be the household of that alert, otherwise nothing is recorded |
| Text `1` / `2`, "a salvo" / "ayuda" | same, against the most recent alert of that phone |
| Location pin | attached (lat/lon/note) to that phone's open case (`reported_lat`, `reported_lon`, `location_note` in `GET /alerts/cases`); acknowledged honestly if there is no open case |
| Voice note (OGG/Opus) | downloaded, converted to 16 kHz mono WAV, transcribed with `muse-voice-transcribe-1.0`, then handled like text (so "estoy a salvo" spoken works). The reply starts with "Entendí: «...»" so the user sees what was heard |
| Video | stored in the clip store and a case of kind `watcher_report` is opened for an operator. **It is not measured**: SAM needs the marker height in cm and a no-people confirmation, so the reply says an operator will review it |
| Other text | Spanish answer from Muse Spark, at most 600 characters, **no tools** (see guardrails). Without `WILLAY_META_ENABLED`, a help menu |
| "hola", "menu" | help menu, no model call |
| Image, document, sticker | short explanation of what is supported |
| Delivery statuses | `sent` / `delivered` / `read` / `failed` update `message_status` of the alert whose message id matches |

Guardrails for model answers (`application/citizen_assistant.py`): the model has no tools and cannot read or change
anything; an answer that names an alert tier or orders an evacuation is replaced by a fixed message pointing to INDECI
(115), SENAMHI and the municipality; every answer carries "Willay complementa a SENAMHI e INDECI". Alert tiers come
only from the domain rules, and an evacuation is only confirmed by a human operator.

The webhook answers `200` immediately and works in a background task. Duplicate deliveries (Meta retries for up to 7
days) are dropped by message id (in memory: lost on restart, like the ledger).

## Voice note decoding

Chosen path in this repository: **ffmpeg from `imageio-ffmpeg`** (already in `requirements.txt`; no new dependency).
If `av` (PyAV) is importable it is used instead; install it yourself if you prefer (`pip install av`, not installed by
default). With neither decoder the bot replies "Por ahora no puedo escuchar audios, escríbame por favor."

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /whatsapp/webhook` | Meta handshake: `hub.mode=subscribe` and the right `hub.verify_token` return `hub.challenge` as plain text, otherwise 403 |
| `POST /whatsapp/webhook` | events; `X-Hub-Signature-256` (HMAC-SHA256 of the raw body, app secret) is mandatory, 403 on mismatch |
| `POST /whatsapp/test` | `{"to": "+51..."}` sends a Spanish hello plus the two buttons to an allow-listed number (403 otherwise, 502 with the Meta code on failure) |
| `GET /whatsapp/log` | recent inbound/outbound events, newest first, phones masked, no message text |

All of them answer 404 while the simulator is active (the log answers with an empty list).

## Costs

Messages sent inside the 24 h window after the user wrote first are **service conversations** (free as of the pricing
Meta publishes for the Cloud API; check the current pricing page). Template messages and business-initiated messages
outside the window are billed per message by category. The test number is free to use but limited to its recipient
list. Voice notes and model answers use the Meta Model API budget (`docs/META_API.md`): transcription is billed per
audio second, answers per token.

## Honesty notes

- Willay **complements** SENAMHI, INDECI and ENFEN and never replaces them. The model answers are general safety advice,
  not alerts.
- Households here are **synthetic**. A real deployment needs informed consent of every recipient and a Meta-approved
  template for business-initiated alerts (the demo relies on the user writing first).
- A button tap or "1" is an unverified self-report; an unanswered alert stays `NO_ANSWER` and escalates to an operator.
- Videos are **not analysed** over WhatsApp.

## Verified and unverified

Verified against the official Meta documentation (2026-11-04): send endpoint and body, reply-button limits (3 buttons,
id up to 256, title up to 20, body up to 1024), template message, two-step media download with the bearer token (URL
valid 5 minutes), status and message webhook envelopes, `X-Hub-Signature-256`. Covered by tests with a fake Graph API; no
request was sent to Meta from this code.

**UNVERIFIED** (no live run yet): that your test number accepts the `hello_world` fallback, the exact name of the user-id
field for messages from users without a phone number (the parser accepts `from_user_id` / `user_id` and never crashes
without a phone), and Graph `v26.0` availability on your app (set `WHATSAPP_GRAPH_VERSION=v25.0` if Meta rejects it).

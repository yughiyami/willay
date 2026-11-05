# Web chat and Telegram channels

WhatsApp needs Meta test-recipient verification. For demos without it, Willay has two more citizen channels that reuse
the same logic as the WhatsApp bot (`CitizenChat`): SAFE/HELP buttons call `record_response`, a location pin lands on the
operator case, a voice note is converted to WAV, transcribed (Muse Voice Transcribe) and answered, and any other text goes
to the citizen assistant. The guardrails do not change: the assistant never names an alert tier or orders an evacuation,
and every answer cites SENAMHI/INDECI.

## How alerts bind to a chat

A chat has no phone number. When alerts are dispatched (`POST /alerts/dispatch`, an approved agent proposal or the demo),
every web chat session and every subscribed Telegram chat acts as one of the dispatched SYNTHETIC households: it is bound
to one alert of that dispatch (alerts nobody has answered come first, then those whose call got no answer; each chat gets a
different alert). The alert text arrives with the buttons `SAFE:<alert_id>` / `HELP:<alert_id>`, so a tap updates the
operator console counts and cases exactly like the simulated phone. The ledger records the answer channel as `webchat` or
`telegram`. A chat can only answer alerts bound to it. At EVACUATION, if an evacuation card image is available (the
recorded card of the offline demo), it is sent as an image too. A demo reset clears the bindings.

## Web chat (always on, no configuration)

In memory, capped at 20 sessions and 300 messages per session. The browser polls.

- `POST /webchat/sessions` body `{"name": str|null}` returns `{session_id, display_name}`
- `GET /webchat/sessions/{id}/messages?after=<seq>` returns `{messages: [{seq, direction in|out, kind, text, buttons, image_url, lat, lon, ts, status}]}`
- `POST /webchat/sessions/{id}/messages` body `{"type":"text","text":...}`, `{"type":"button","id":"SAFE:alert-0003"}` or `{"type":"location","lat":..,"lon":..}` returns `{"accepted": true}`; replies are produced in a background task and show up by polling
- `POST /webchat/sessions/{id}/audio` multipart field `file` (browser MediaRecorder webm/ogg opus) returns `{accepted, transcript}`; the transcript is stored as an `in` audio message and then answered like text
- `GET /webchat/media/{name}` serves the images referenced by `image_url` (a path relative to the API base)

## Telegram (long polling, no webhook, no tunnel)

1. In Telegram open `@BotFather`, send `/newbot`, pick a name and a username ending in `bot`, and copy the token.
2. Put it in the repo-root `.env` (never commit it):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   # optional: comma-separated chat ids that are served without sending /start (otherwise any chat that sends /start, max 10)
   TELEGRAM_ALLOWED_CHAT_IDS=
   ```
3. Restart the backend. With a token set, a daemon thread calls `getUpdates` (timeout 25 s) and `GET /health` reports
   `telegram_mode: "live (n chats)"`; without it, `"disabled: TELEGRAM_BOT_TOKEN missing"`.
4. Open your bot in Telegram and send `/start`. Dispatch an alert from the console: the message arrives with the two
   inline buttons. Try also a location pin (attachment menu), a voice note and a free question.

`GET /telegram/status` returns `{enabled, chats, last_error}` (chat ids are masked and the token is never shown or logged).
Subscriptions live in memory: after a backend restart send `/start` again (or list your chat id in
`TELEGRAM_ALLOWED_CHAT_IDS` so any message re-subscribes). Only one process can poll a bot at a time (a second one gets
HTTP 409 in `last_error`). A failure on one chat or channel never breaks the dispatch or the other channels.

## Fan-out

Dispatch delivers to the simulated phone mock-up (always), WhatsApp (when configured), the web chat and Telegram (when
enabled). Each is isolated: an exception on one channel is swallowed and the rest still get the alert.

## Tests

`backend/tests/api/test_api_webchat.py` and `backend/tests/unit/test_telegram_bot.py` (fake Bot API through
`httpx.MockTransport`; no network).

# Telephony: automated voice calls through Asterisk ARI

Willay Relaves can call a household, play a prerecorded Spanish message for the alert tier and collect one key press
(**1 = I am safe, 2 = I need help**). It does this through the **Asterisk REST Interface (ARI)** using **your own SIP
trunk**. Nothing here uses a third-party calling service.

> **Safety first**
>
> - Calls are **simulated by default**. A real call is possible only when `WILLAY_CALLS_ENABLED=1` **and** every
>   Asterisk variable below is set. Otherwise the app keeps `SimulatedCallAdapter` (no call is ever placed) and
>   `GET /health` reports `calls_mode` with the NAMES of what is missing.
> - **Allow-list.** `AsteriskAriAdapter` refuses (`CallNotAllowed`, before any network activity) any number that is
>   not a valid E.164 number or not listed in `WILLAY_CALL_ALLOWLIST`. An empty list refuses everything. A malformed
>   entry in the list is a configuration error (the app stays simulated), never silently skipped.
> - The dispatch endpoints only contact **synthetic demo households** from `backend/fixtures/households_synthetic.json`.
>   To hear a real phone ring, copy that file to a LOCAL path (outside git), put your own number in it, add the same
>   number to `WILLAY_CALL_ALLOWLIST` and point `WILLAY_DEMO_HOUSEHOLDS_FILE` at the copy.
> - `EVACUATION` dispatch needs an operator confirmation id (`POST /alerts/dispatch`), exactly like the tier rules.
> - Credentials are read from the environment only; they never appear in a URL, a log line or an API response.

## Environment variables (names only; put values in your local `.env`, never in git)

| Variable | Default | Purpose |
|---|---|---|
| `WILLAY_CALLS_ENABLED` | `0` | `1` enables real calls (still simulated if the rest is missing) |
| `WILLAY_CALL_ALLOWLIST` | empty | comma-separated E.164 numbers that may be dialled, for example `+51999000111,+51999000222` |
| `ASTERISK_ARI_URL` | none | base URL of the Asterisk HTTP server, for example `http://127.0.0.1:8088` |
| `ASTERISK_ARI_USER` | none | ARI user (`ari.conf` section name) |
| `ASTERISK_ARI_PASSWORD` | none | ARI password |
| `ASTERISK_TRUNK_NAME` | none | name of the PJSIP endpoint of your trunk; the dial string is `PJSIP/<number>@<trunk>` |
| `ASTERISK_ARI_APP` | `willay` | Stasis application name |
| `WILLAY_CALL_DTMF_TIMEOUT_S` | `30` | seconds to wait for key 1 or 2 after the recording starts, then `NO_ANSWER` |
| `WILLAY_CALL_RING_TIMEOUT_S` | `30` | seconds the phone may ring before the call counts as unanswered |
| `WILLAY_CALL_SOUND_PREPARATION` / `_ATTENTION` / `_EVACUATION` | `sound:willay/preparacion` / `atencion` / `evacuacion` | Asterisk media URI played per tier |
| `WILLAY_DEMO_HOUSEHOLDS_FILE` | bundled fixture | local households JSON (same format as the fixture) |

The adapter needs the `websockets` package (pinned in `requirements.txt`, imported lazily; tests do not need it).

## What one call does

1. The ARI events WebSocket is opened first, at `ws://<host>/ari/events?app=<app>` with an HTTP Basic header.
2. `POST /ari/channels` with `endpoint=PJSIP/<number>@<trunk>`, `app=<app>`, `appArgs=<TIER>`, a generated
   `channelId` and `timeout=<ring timeout>` originates the call straight into the Stasis application.
3. `StasisStart` for that channel means the person answered. The tier recording is played with
   `POST /ari/channels/{id}/play?media=sound:willay/<tier>`.
4. `ChannelDtmfReceived`: `1` = `SAFE`, `2` = `NEEDS_HELP`; other keys are ignored (keys pressed while the
   recording plays count). No key within the DTMF timeout, no answer, or a hang-up = `NO_ANSWER`.
5. The channel is always hung up with `DELETE /ari/channels/{id}` and the event stream closed.

`NEEDS_HELP` and `NO_ANSWER` open an **operator case** (`GET /alerts/cases`). A number that is blocked by the
allow-list, or a telephony failure, is also escalated as `NO_ANSWER` (with the reason in the case note): an
unconfirmed household is never treated as safe.

## Asterisk configuration (placeholders only)

`http.conf`: enable the built-in HTTP server for ARI. Bind to localhost unless you secure the link.

```ini
[general]
enabled = yes
bindaddr = 127.0.0.1
bindport = 8088
```

`ari.conf`: one ARI user for Willay.

```ini
[general]
enabled = yes
pretty = no

[<ARI_USER>]
type = user
read_only = no
password = <ARI_PASSWORD>
password_format = plain
```

`pjsip.conf`: your own trunk. Every value below is a placeholder; copy the settings your SIP provider documents.
The section name of the endpoint (`my-trunk` here) is what you put in `ASTERISK_TRUNK_NAME`.

```ini
[transport-udp]
type = transport
protocol = udp
bind = 0.0.0.0

[my-trunk]
type = endpoint
transport = transport-udp
context = willay-stasis
disallow = all
allow = alaw,ulaw
aors = my-trunk
outbound_auth = my-trunk-auth
from_user = <SIP_USERNAME>
from_domain = <PROVIDER_DOMAIN>
direct_media = no

[my-trunk]
type = aor
contact = sip:<PROVIDER_HOST>:5060

[my-trunk-auth]
type = auth
auth_type = userpass
username = <SIP_USERNAME>
password = <SIP_PASSWORD>

[my-trunk]
type = identify
endpoint = my-trunk
match = <PROVIDER_IP>

; Only if your provider requires registration:
[my-trunk-reg]
type = registration
transport = transport-udp
outbound_auth = my-trunk-auth
server_uri = sip:<PROVIDER_HOST>
client_uri = sip:<SIP_USERNAME>@<PROVIDER_HOST>
retry_interval = 60
```

`extensions.conf`: the originate request already enters the Stasis application, so no dialplan is needed for
outbound calls. This context is only a safe landing for the trunk endpoint (`context = willay-stasis`) and a manual
test (`Local/s@willay-stasis`):

```ini
[willay-stasis]
exten => s,1,NoAnswer()
 same => n,Stasis(willay)
 same => n,Hangup()
```

Reload with `asterisk -rx "core reload"` and check with `asterisk -rx "pjsip show endpoints"` and
`curl -u <ARI_USER>:<ARI_PASSWORD> http://127.0.0.1:8088/ari/asterisk/info`.

## Recording the tier messages

One recording per tier that makes calls (`ATTENTION`, `EVACUATION`; `PREPARATION` is optional). Keep them short:
two plain sentences, then the keys. Suggested Spanish scripts (they mirror the approved written messages; keep the
official-source mention):

- `atencion`: "Atención. Pueden venir lluvias fuertes en uno a tres días. Esté listo para salir. Esta alerta complementa
  a SENAMHI, INDECI y ENFEN. Si está a salvo, marque uno. Si necesita ayuda, marque dos."
- `evacuacion`: "Evacúe ahora al punto seguro. No cruce el río. Esta alerta complementa a SENAMHI, INDECI y ENFEN.
  Si está a salvo, marque uno. Si necesita ayuda, marque dos."

Format: **WAV, mono, 16-bit PCM, 8 kHz** (telephone quality; 16 kHz also works if the trunk codec is wideband).
Convert any recording with `ffmpeg` or `sox` (these tools are NOT installed by this project):

```bash
ffmpeg -i evacuacion_raw.wav -ar 8000 -ac 1 -sample_fmt s16 evacuacion.wav
# or: sox evacuacion_raw.wav -r 8000 -c 1 -b 16 evacuacion.wav
```

Place the files in the Asterisk sounds directory, for example `/var/lib/asterisk/sounds/es/willay/` (language
subfolder `es` when the channel language is `es`) or `/var/lib/asterisk/sounds/willay/`. The media URI has **no
extension**: `sound:willay/evacuacion`. An absolute path also works: `sound:/var/lib/asterisk/sounds/custom/evac`.
Override per tier with `WILLAY_CALL_SOUND_<TIER>`.

**Quechua.** Never use text-to-speech or machine translation for Quechua. Record it with a native speaker and have a
second native speaker (ideally from the target community) review the wording, the pronunciation and the key
instructions before any use; keep the reviewer's name and date next to the files, as for the written templates
(`backend/templates/reviewed/<lang>.json`). Until then, calls play Spanish only.

## Not verified against a live Asterisk

The adapter is fully tested against a mocked HTTP transport and a fake event stream, but it has **not** been run against a
real Asterisk and a real trunk. Check these first on your own server:

- the WebSocket handshake accepts the HTTP Basic header (if your Asterisk only accepts `?api_key=<user>:<password>`,
  the adapter needs a small change; the credentials would then travel in the URL);
- the `timeout` query parameter of `POST /channels` is honoured for your Asterisk version;
- `StasisStart` really arrives only when the callee answers on your trunk (some providers answer early with
  early media), and `ChannelDtmfReceived` fires for RFC 4733 and in-band tones as your trunk delivers them;
- the recordings play from the media URI you configured.

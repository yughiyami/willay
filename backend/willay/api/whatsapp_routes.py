"""WhatsApp Cloud API webhook, operator test send and event log.

Every route answers 404 unless WhatsApp is the active messaging provider (WILLAY_MESSAGING_PROVIDER=whatsapp, fully
configured), except GET /whatsapp/log which then just reports the mode with no events. The POST webhook validates
`X-Hub-Signature-256` over the raw body, answers 200 immediately and does the work (media download, transcription,
model call) in a background task, because Meta retries slow or failed webhooks for days.
"""

from __future__ import annotations

import json

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from willay.adapters.whatsapp_cloud import WhatsAppCloudMessenger, parse_webhook_payload, to_wa_id
from willay.api.dependencies import AppDependencies
from willay.application.citizen_assistant import CitizenAssistant
from willay.application.whatsapp_inbound import WhatsAppInboundHandler
from willay.domain.dispatch import mask_phone
from willay.ports.messaging import InteractiveMessagingPort, MessageError, MessageNotAllowed

TEST_BODY = "Hola, soy Willay Relaves. Esta es una prueba del canal de WhatsApp. ¿Puede confirmar que recibió este mensaje?"
TEST_BUTTONS = [("TEST:OK", "Estoy a salvo"), ("TEST:HELP", "Necesito ayuda")]
TEMPLATE_NOTE = (
    "Outside the 24 h window: only the generic hello_world template was sent, not the test text. "
    "Ask the recipient to message the bot first (for example 'hola') and try again."
)


class WhatsAppTestIn(BaseModel):
    to: str = Field(min_length=1, max_length=32, description="Allow-listed phone in E.164, for example +51999000111")


def register_whatsapp_routes(app: FastAPI, deps: AppDependencies) -> None:
    messenger: WhatsAppCloudMessenger | None = deps.whatsapp
    handler: WhatsAppInboundHandler | None = None
    if messenger is not None:
        handler = WhatsAppInboundHandler(
            ledger=deps.ledger,
            messaging=deps.messaging,
            households=tuple(deps.households),
            is_allowed=messenger.is_allowed,
            media=messenger,
            audio_converter=deps.audio_converter,  # type: ignore[arg-type]
            transcriber=deps.transcriber,
            assistant=CitizenAssistant(deps.agent_model) if deps.agent_model is not None else None,
            clip_sink=lambda data, mime: deps.clips.add(f"wa-clip-{len(deps.clips.ids()) + 1}", data, mime),
            log=messenger.log,
        )

    def active() -> tuple[WhatsAppCloudMessenger, WhatsAppInboundHandler]:
        if messenger is None or handler is None:
            raise HTTPException(status_code=404, detail="WhatsApp is not the active messaging provider")
        return messenger, handler

    @app.get("/whatsapp/webhook")
    def verify(
        mode: str | None = Query(default=None, alias="hub.mode"),
        verify_token: str | None = Query(default=None, alias="hub.verify_token"),
        challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        channel, _ = active()
        if mode != "subscribe" or not channel.verify_token_matches(verify_token) or challenge is None:
            channel.log.record("in", "verify", "", "webhook verification refused (wrong mode or verify token)")
            raise HTTPException(status_code=403, detail="webhook verification failed")
        channel.log.record("in", "verify", "", "webhook verified by Meta")
        return PlainTextResponse(challenge)

    @app.post("/whatsapp/webhook")
    async def receive(request: Request, background: BackgroundTasks) -> dict:
        channel, inbound = active()
        raw = await request.body()  # the signature covers the RAW bytes, so read them before parsing
        if not channel.validate_signature(raw, request.headers.get("X-Hub-Signature-256")):
            channel.log.record("in", "webhook", "", "rejected: invalid X-Hub-Signature-256")
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="the body is not valid JSON") from exc
        messages, statuses = parse_webhook_payload(payload)
        for message in messages:
            background.add_task(inbound.handle_message, message)
        for status in statuses:
            background.add_task(inbound.handle_status, status)
        return {"status": "ok", "messages": len(messages), "statuses": len(statuses)}

    @app.post("/whatsapp/test")
    def send_test(request: WhatsAppTestIn) -> dict:
        channel, _ = active()
        sender = deps.messaging if isinstance(deps.messaging, InteractiveMessagingPort) else channel
        try:
            result = sender.send_buttons(request.to, TEST_BODY, TEST_BUTTONS)
        except MessageNotAllowed as refused:
            raise HTTPException(status_code=403, detail=str(refused)) from refused
        except MessageError as failed:
            raise HTTPException(status_code=502, detail=str(failed)) from failed
        return {
            "sent": True,
            "to_masked": mask_phone("+" + to_wa_id(request.to)),
            "used_template": result.used_template,
            "note": TEMPLATE_NOTE if result.used_template else "Sent with two reply buttons.",
        }

    @app.get("/whatsapp/log")
    def log(limit: int = Query(default=50, ge=1, le=200)) -> dict:
        return {"messaging_mode": deps.messaging_mode, "events": messenger.log.recent(limit) if messenger is not None else []}

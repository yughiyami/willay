"""Telephony webhooks. Only the Twilio gather callback today; it answers 404 unless Twilio is the active provider."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from willay.adapters.twilio_calls import GATHER_PATH, TwilioCallAdapter
from willay.api.dependencies import AppDependencies


def register_telephony_routes(app: FastAPI, deps: AppDependencies) -> None:
    @app.post(GATHER_PATH)
    async def twilio_gather(request: Request) -> Response:
        adapter = deps.calls
        if not isinstance(adapter, TwilioCallAdapter):
            raise HTTPException(status_code=404, detail="Twilio is not the active calls provider")
        form = await request.form()
        params = {key: str(value) for key, value in form.items()}
        # The signature covers the PUBLIC url Twilio called (the tunnel address), not the local Host header.
        url = adapter.public_url(request.url.path, request.url.query)
        if not adapter.validate_signature(url, params, request.headers.get("X-Twilio-Signature")):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        status = adapter.receive_digits(params.get("CallSid", ""), params.get("Digits", ""))
        return Response(content=adapter.webhook_twiml(status), media_type="text/xml")

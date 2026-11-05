"""Web chat over HTTP: a WhatsApp-like conversation in the browser (always enabled, in memory).

POST /webchat/sessions                      -> {session_id, display_name}
GET  /webchat/sessions/{id}/messages?after= -> {messages: [...]}   (the browser polls)
POST /webchat/sessions/{id}/messages        -> {accepted: true}    text | button | location; the reply arrives by polling
POST /webchat/sessions/{id}/audio           -> multipart "file" (webm/ogg opus): transcribed, then answered like text
GET  /webchat/media/{name}                  -> image referenced by a message's image_url
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from willay.adapters.webchat import WebChatStore
from willay.application.citizen_chat import ChatReply, CitizenChat

MAX_AUDIO_UPLOAD_BYTES = 5_000_000


class SessionIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)


class TextIn(BaseModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=1000)


class ButtonIn(BaseModel):
    type: Literal["button"]
    id: str = Field(min_length=1, max_length=80)


class LocationIn(BaseModel):
    type: Literal["location"]
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


MessageIn = Annotated[Union[TextIn, ButtonIn, LocationIn], Field(discriminator="type")]


class WebChatService:
    """Turns the citizen's input into stored messages and Willay's replies (runs in a background task)."""

    def __init__(self, store: WebChatStore, chat: CitizenChat) -> None:
        self.store, self.chat = store, chat

    def send(self, session_id: str, replies: list[ChatReply]) -> None:
        for reply in replies:
            self.store.add(session_id, "out", "buttons" if reply.buttons else "text", text=reply.text, buttons=reply.buttons or None)

    def on_text(self, session_id: str, text: str, spoken: bool = False) -> None:
        first = self.store.first_contact(session_id)
        replies = self.chat.text(self.store.bound_alerts(session_id), text, spoken=spoken)
        if first and replies and replies[0].text != self.chat.welcome().text:
            replies = [self.chat.welcome(), *replies]
        self.send(session_id, replies)

    def on_button(self, session_id: str, button_id: str) -> None:
        self.store.first_contact(session_id)
        self.send(session_id, self.chat.button(self.store.bound_alerts(session_id), button_id))

    def on_location(self, session_id: str, lat: float, lon: float) -> None:
        self.store.first_contact(session_id)
        self.send(session_id, self.chat.location(self.store.bound_alerts(session_id), lat, lon, "web chat"))


def register_webchat_routes(app: FastAPI, store: WebChatStore, service: WebChatService) -> None:
    def require(session_id: str):
        session = store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown chat session")
        return session

    @app.post("/webchat/sessions")
    def create_session(request: SessionIn | None = None) -> dict:
        session = store.create_session(request.name if request else None)
        return {"session_id": session.id, "display_name": session.display_name}

    @app.get("/webchat/sessions/{session_id}/messages")
    def messages(session_id: str, after: int = Query(default=0, ge=0)) -> dict:
        found = store.messages_after(session_id, after)
        if found is None:
            raise HTTPException(status_code=404, detail="unknown chat session")
        return {"messages": found}

    @app.post("/webchat/sessions/{session_id}/messages")
    def post_message(session_id: str, request: MessageIn, background: BackgroundTasks) -> dict:
        require(session_id)
        if isinstance(request, TextIn):
            store.add(session_id, "in", "text", text=request.text, status="delivered")
            background.add_task(service.on_text, session_id, request.text)
        elif isinstance(request, ButtonIn):
            title = store.button_title(session_id, request.id) or request.id
            store.add(session_id, "in", "text", text=title, status="delivered")
            background.add_task(service.on_button, session_id, request.id)
        else:
            store.add(session_id, "in", "location", text="Ubicación compartida", lat=request.lat, lon=request.lon, status="delivered")
            background.add_task(service.on_location, session_id, request.lat, request.lon)
        return {"accepted": True}

    @app.post("/webchat/sessions/{session_id}/audio")
    async def post_audio(session_id: str, background: BackgroundTasks, file: UploadFile = File(...)) -> dict:
        require(session_id)
        data = await file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="the audio is empty")
        if len(data) > MAX_AUDIO_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="the audio is too long")
        outcome = await run_in_threadpool(service.chat.audio, data, file.content_type or "audio/webm")
        store.add(session_id, "in", "audio", text=outcome.transcript or None, status="delivered")
        if outcome.transcript:
            background.add_task(service.on_text, session_id, outcome.transcript, True)
        else:
            service.store.first_contact(session_id)
            service.send(session_id, outcome.replies)
        return {"accepted": True, "transcript": outcome.transcript}

    @app.get("/webchat/media/{name}")
    def media(name: str) -> Response:
        found = store.get_media(name)
        if found is None:
            raise HTTPException(status_code=404, detail="unknown media")
        data, mime = found
        return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


"""POST /agent/text and POST /agent/voice: the operator's Spanish assistant that drives the map."""

from __future__ import annotations

from typing import Annotated, Callable, Sequence

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from willay.adapters.meta_model_api import MetaApiError, UnsupportedAudioError
from willay.adapters.meta_usage import BudgetExceededError
from willay.api.dependencies import AppDependencies
from willay.application.operator_agent import OperatorAgent, OperatorToolkit
from willay.application.rank_sites import RankedSite

MAX_AUDIO_BYTES = 5_000_000  # ~2.5 minutes of 16 kHz mono 16-bit PCM


class AgentTextIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    previous_response_id: str | None = Field(default=None, max_length=200)


def register_agent_routes(app: FastAPI, deps: AppDependencies, ranked: Callable[[], Sequence[RankedSite]]) -> None:
    def run_agent(transcript: str, previous_response_id: str | None) -> dict:
        assert deps.agent_model is not None
        toolkit = OperatorToolkit(ranked, deps.hazard_scenario, districts=deps.district_collection)
        try:
            reply = OperatorAgent(deps.agent_model, toolkit).answer(transcript, previous_response_id)
        except BudgetExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MetaApiError as exc:
            raise HTTPException(status_code=502, detail=f"the language model failed: {exc}") from exc
        return {
            "transcript": transcript,
            "answer_es": reply.answer_es,
            "ui_actions": list(reply.ui_actions),
            "tools_used": [c["name"] for c in reply.tool_calls],
            "flags": list(reply.flags),
            "response_id": reply.response_id,
        }

    @app.post("/agent/text")
    def agent_text(request: AgentTextIn) -> dict:
        if deps.agent_model is None:
            raise HTTPException(status_code=503, detail="the agent model is not configured (set WILLAY_META_ENABLED=1 and META_API_KEY)")
        return run_agent(request.text.strip(), request.previous_response_id)

    @app.post("/agent/voice")
    def agent_voice(
        audio: Annotated[UploadFile, File(description="WAV: mono, 16-bit PCM, 16 or 24 kHz, at most 10 minutes")],
        previous_response_id: Annotated[str | None, Form()] = None,
    ) -> dict:
        if deps.agent_model is None or deps.transcriber is None:
            raise HTTPException(status_code=503, detail="voice is not configured (set WILLAY_META_ENABLED=1 and META_API_KEY)")
        data = audio.file.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio is larger than 5 MB")
        try:
            transcript = deps.transcriber.transcribe(data, "es").strip()
        except UnsupportedAudioError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except BudgetExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except MetaApiError as exc:
            raise HTTPException(status_code=502, detail=f"transcription failed: {exc}") from exc
        if not transcript:
            raise HTTPException(status_code=422, detail="no speech was detected in the recording")
        return run_agent(transcript, previous_response_id)

"""Skeleton adapter for the Meta model API (OpenAI-compatible base URL).

Models: muse-spark-1.3 (text), muse-voice-transcribe-1.0 (speech-to-text),
sam-3.1 (segmentation), muse-image-1.0 (images).

The API key is read from the environment (META_API_KEY) and is never logged or echoed.
Language models here only rephrase or explain; they never decide alert tiers.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import Mapping

import httpx

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
MODEL_TEXT = "muse-spark-1.3"
MODEL_TRANSCRIBE = "muse-voice-transcribe-1.0"
MODEL_SEGMENTATION = "sam-3.1"
MODEL_IMAGE = "muse-image-1.0"

_REPHRASE_SYSTEM_PROMPT = (
    "You rephrase early-warning messages for communities. Do not change any fact: keep the alert "
    "tier, site name, distances, safe point and official source exactly as given, and keep the "
    "statement that this alert complements official SENAMHI, INDECI and ENFEN alerts. "
    "Never raise, lower or invent an alert level. Use short, plain sentences."
)
_EXPLAIN_SYSTEM_PROMPT = (
    "You explain a risk score breakdown in plain language. Only use the numbers given. "
    "Do not change or invent alert levels or scores."
)


class MissingApiKeyError(RuntimeError):
    """META_API_KEY is not set."""


class MetaApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MetaModelApiConfig:
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MetaModelApiConfig":
        env = os.environ if environ is None else environ
        key = (env.get("META_API_KEY") or "").strip()
        if not key:
            raise MissingApiKeyError("META_API_KEY is not set (see .env.example)")
        base_url = (env.get("META_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        return cls(api_key=key, base_url=base_url)


class MetaModelApiAdapter:
    """Implements LLMPort, TranscriptionPort and ImagePort over HTTP. Segmentation is a skeleton."""

    def __init__(self, config: MetaModelApiConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=30.0)

    # --- plumbing ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.api_key}"}

    def _post(self, path: str, **kwargs) -> dict:
        try:
            response = self._client.post(f"{self._config.base_url}{path}", headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise MetaApiError(f"request to {path} failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise MetaApiError(f"{path} returned HTTP {response.status_code}", status_code=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise MetaApiError(f"{path} returned a non-JSON body") from exc

    def _chat(self, system_prompt: str, user_content: str) -> str:
        payload = self._post(
            "/chat/completions",
            json={
                "model": MODEL_TEXT,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
            },
        )
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MetaApiError("unexpected chat completion response shape") from exc

    # --- LLMPort ----------------------------------------------------------------

    def rephrase(self, text: str, language: str = "en") -> str:
        return self._chat(_REPHRASE_SYSTEM_PROMPT, f"Language: {language}\n\n{text}")

    def explain(self, facts: str, language: str = "en") -> str:
        return self._chat(_EXPLAIN_SYSTEM_PROMPT, f"Language: {language}\n\n{facts}")

    # --- TranscriptionPort -------------------------------------------------------

    def transcribe(self, audio: bytes, language: str = "es") -> str:
        payload = self._post(
            "/audio/transcriptions",
            data={"model": MODEL_TRANSCRIBE, "language": language},
            files={"file": ("report.audio", audio, "application/octet-stream")},
        )
        try:
            return payload["text"]
        except (KeyError, TypeError) as exc:
            raise MetaApiError("unexpected transcription response shape") from exc

    # --- ImagePort ----------------------------------------------------------------

    def generate(self, prompt: str) -> bytes:
        payload = self._post("/images/generations", json={"model": MODEL_IMAGE, "prompt": prompt, "response_format": "b64_json"})
        try:
            return base64.b64decode(payload["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MetaApiError("unexpected image generation response shape") from exc

    # --- SegmentationPort (skeleton) ------------------------------------------------

    def measure_water_rise_cm(self, reference_frame: bytes, current_frame: bytes) -> float | None:
        """Skeleton: the sam-3.1 request contract is not confirmed yet.

        TODO: segment the water mask in both frames, convert the mask delta to centimeters with
        the site's calibration (known gauge height in pixels), and return the rise.
        """
        raise NotImplementedError("sam-3.1 segmentation request contract is not confirmed yet")

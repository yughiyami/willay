"""Adapter for the Meta Model API (https://dev.meta.ai/docs, contract recorded in docs/META_API.md).

Endpoints used (base URL https://api.meta.ai/v1):
  POST /responses                text (muse-spark-*), structured output, tool calling, and sam-3.1 segmentation
  POST /asr/transcribe           muse-voice-transcribe-1.0 (multipart: `request` JSON part + `audio` WAV part)
  POST /images/generations       muse-image-1.0

The API key is read from the environment (META_API_KEY) and is never logged, echoed or placed in an
error message. Language models here only rephrase, explain, extract or call tools; they never decide
alert tiers. Every live call is appended to the usage log and checked against a spend budget first.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import wave
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping, Sequence

import httpx

from willay.adapters.meta_usage import UsageLog, asr_cost, image_cost, sam_cost, text_cost
from willay.domain.segmentation import FrameSegmentation, Mask, TrackedObject, VideoSegmentation
from willay.ports.agent import AgentTurn, ToolCall

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
MODEL_TEXT = "muse-spark-1.3"  # audio-free text/agent work; 1.3 audio input is "not fully supported"
MODEL_TEXT_AUDIO = "muse-spark-1.2"  # use this one if audio is ever sent to a Spark model
MODEL_TRANSCRIBE = "muse-voice-transcribe-1.0"
MODEL_SEGMENTATION = "sam-3.1"
MODEL_IMAGE = "muse-image-1.0"
MODEL_JUDGE = "muse-wonder-1.0"

_RETRY_STATUSES = (429, 500, 502, 503, 504)
_RETRY_BACKOFF_S = 1.0
_RESPONSES_RETRIES = 2

ASR_MAX_SECONDS = 600
ASR_RATES = (16000, 24000)
INLINE_MEDIA_LIMIT_BYTES = 40_000_000  # the documented inline limit is 50 MB; keep headroom for base64 overhead
DEFAULT_REASONING_EFFORT = "low"
_FLAT_FILE_TOKEN_ESTIMATE = 15_000  # a PDF/video part is priced by its content, not by its base64 length

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


class UnsupportedAudioError(ValueError):
    """The audio is not a mono 16-bit PCM WAV at 16 or 24 kHz of at most 10 minutes."""


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


def _response_text(payload: Mapping) -> str:
    parts: list[str] = []
    for item in payload.get("output") or []:
        if item.get("type") == "message":
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    parts.append(block.get("text", ""))
    return "".join(parts)


def _wav_info(audio: bytes) -> float:
    """Validate the WAV and return its duration in seconds."""
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
    except (wave.Error, EOFError) as exc:
        raise UnsupportedAudioError("audio must be a PCM WAV file") from exc
    if channels != 1 or width != 2 or rate not in ASR_RATES:
        raise UnsupportedAudioError("WAV must be mono, 16-bit PCM at 16 kHz or 24 kHz")
    seconds = frames / rate
    if seconds > ASR_MAX_SECONDS:
        raise UnsupportedAudioError("audio is longer than the 10 minute limit")
    return seconds


_FRAME_LINE = re.compile(r"^<\d+f>")
_SIZE = re.compile(r"w=(\d+);h=(\d+)")


class MetaModelApiAdapter:
    """Implements LLMPort, AgentModelPort, StructuredExtractionPort, TranscriptionPort, ImagePort and
    VideoSegmentationPort over HTTP."""

    def __init__(
        self,
        config: MetaModelApiConfig,
        client: httpx.Client | None = None,
        usage_log: UsageLog | None = None,
        text_model: str = MODEL_TEXT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._config = config
        self._client = client or httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0))
        self._usage = usage_log
        self._text_model = text_model

    @property
    def usage_log(self) -> UsageLog | None:
        return self._usage

    # --- plumbing ---------------------------------------------------------------

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        headers.update(extra or {})
        return headers

    def _redact(self, text: str) -> str:
        return text.replace(self._config.api_key, "[redacted]") if self._config.api_key else text

    def _http_error(self, path: str, response: httpx.Response) -> MetaApiError:
        detail = ""
        try:
            error = response.json().get("error")
            if isinstance(error, Mapping):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
        except (ValueError, AttributeError):
            pass
        message = f"{path} returned HTTP {response.status_code}" + (f": {detail}" if detail else "")
        return MetaApiError(self._redact(message), status_code=response.status_code)

    def _guard(self, estimated_cost: float) -> None:
        if self._usage is not None:
            self._usage.ensure_within_budget(estimated_cost)

    def _log(self, **fields: object) -> None:
        if self._usage is not None:
            self._usage.record(**fields)

    @staticmethod
    def _is_transient(response: httpx.Response) -> bool:
        """Rate limits and server errors, plus a response id that was created a moment ago and is not visible yet."""
        if response.status_code in _RETRY_STATUSES:
            return True
        return response.status_code == 400 and "referenced response not found" in response.text.lower()

    def _post(self, path: str, *, estimated_cost: float = 0.0, retries: int = 0, **kwargs) -> dict:
        self._guard(estimated_cost)
        headers = self._headers(kwargs.pop("headers", None))
        attempt = 0
        while True:
            try:
                response = self._client.post(f"{self._config.base_url}{path}", headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise MetaApiError(self._redact(f"request to {path} failed: {type(exc).__name__}")) from exc
            if response.status_code < 400:
                break
            if attempt < retries and self._is_transient(response):
                attempt += 1
                self._sleep(_RETRY_BACKOFF_S * attempt)  # 1 s, then 2 s: exponential enough for two retries
                continue
            raise self._http_error(path, response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetaApiError(f"{path} returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise MetaApiError(f"{path} returned an unexpected body")
        return payload

    def _sse_events(self, path: str, body: dict, estimated_cost: float) -> Iterator[dict]:
        self._guard(estimated_cost)
        try:
            with self._client.stream(
                "POST", f"{self._config.base_url}{path}", headers=self._headers({"Accept": "text/event-stream"}), json=body
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise self._http_error(path, response)
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue
        except httpx.HTTPError as exc:
            raise MetaApiError(self._redact(f"stream from {path} failed: {type(exc).__name__}")) from exc

    # --- Responses API: text ---------------------------------------------------------------

    @staticmethod
    def _estimate_text_cost(model: str, body: Mapping) -> float:
        payload = json.dumps(body, ensure_ascii=False)
        payload = re.sub(r'"(file_data|video_url|image_url)":\s*"[^"]*"', '"x": ""', payload)
        files = len(re.findall(r'"(?:file_data|video_url|image_url)"', json.dumps(body)))
        input_tokens = len(payload) // 3 + files * _FLAT_FILE_TOKEN_ESTIMATE
        output_tokens = int(body.get("max_output_tokens") or 6000)
        return text_cost(model, input_tokens, output_tokens)

    def _responses(self, body: dict, purpose: str) -> dict:
        model = body["model"]
        payload = self._post("/responses", json=body, estimated_cost=self._estimate_text_cost(model, body), retries=_RESPONSES_RETRIES)
        usage = payload.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0))
        self._log(
            model=model,
            endpoint="/responses",
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            est_cost_usd=text_cost(model, input_tokens, output_tokens, cached),
        )
        return payload

    def _text_request(self, instructions: str, user_text: str, purpose: str) -> str:
        payload = self._responses(
            {
                "model": self._text_model,
                "instructions": instructions,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": user_text}]}],
                "reasoning": {"effort": DEFAULT_REASONING_EFFORT},
            },
            purpose,
        )
        if not isinstance(payload.get("output"), list):
            raise MetaApiError("unexpected response shape")
        text = _response_text(payload)
        if not text:
            raise MetaApiError("the model returned no text")
        return text

    # --- LLMPort ----------------------------------------------------------------

    def rephrase(self, text: str, language: str = "en") -> str:
        return self._text_request(_REPHRASE_SYSTEM_PROMPT, f"Language: {language}\n\n{text}", "rephrase")

    def explain(self, facts: str, language: str = "en") -> str:
        return self._text_request(_EXPLAIN_SYSTEM_PROMPT, f"Language: {language}\n\n{facts}", "explain")

    # --- StructuredExtractionPort -------------------------------------------------

    def extract_json(
        self,
        *,
        instructions: str,
        content: Sequence[Mapping[str, object]],
        schema_name: str,
        schema: Mapping[str, object],
        purpose: str = "extract",
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> dict:
        body: dict = {
            "model": model or self._text_model,
            "instructions": instructions,
            "input": [{"role": "user", "content": list(content)}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": dict(schema), "strict": False}},
            "reasoning": {"effort": DEFAULT_REASONING_EFFORT},
        }
        if max_output_tokens:
            body["max_output_tokens"] = max_output_tokens
        payload = self._responses(body, purpose)
        text = _response_text(payload)
        try:
            result = json.loads(text)
        except ValueError as exc:
            raise MetaApiError("structured output was not valid JSON") from exc
        if not isinstance(result, dict):
            raise MetaApiError("structured output was not a JSON object")
        return result

    # --- AgentModelPort -----------------------------------------------------------

    def run_turn(
        self,
        *,
        instructions: str,
        input_items: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]],
        previous_response_id: str | None = None,
        purpose: str = "agent",
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> AgentTurn:
        body: dict = {
            "model": model or self._text_model,
            "instructions": instructions,
            "input": list(input_items),
            "reasoning": {"effort": DEFAULT_REASONING_EFFORT},
        }
        if max_output_tokens:
            body["max_output_tokens"] = max_output_tokens
        if tools:
            body["tools"] = list(tools)
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        payload = self._responses(body, purpose)
        calls: list[ToolCall] = []
        for item in payload.get("output") or []:
            if item.get("type") == "function_call":
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                calls.append(
                    ToolCall(str(item.get("call_id", "")), str(item.get("name", "")), arguments if isinstance(arguments, dict) else {})
                )
        usage = payload.get("usage") or {}
        return AgentTurn(
            response_id=str(payload.get("id", "")),
            text=_response_text(payload),
            tool_calls=tuple(calls),
            model=str(body["model"]),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cached_tokens=int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0)),
        )

    # --- TranscriptionPort -------------------------------------------------------

    def transcribe(self, audio: bytes, language: str = "es") -> str:
        seconds = _wav_info(audio)
        languages = {"es": "Spanish", "en": "English", "qu": "Spanish", "pt": "Portuguese"}
        settings = {
            "model": MODEL_TRANSCRIBE,
            "audioEncoding": "WAV",
            "mode": "PUSH_TO_TALK",
            "languageBias": [languages.get(language, "Spanish")],
        }
        payload = self._post(
            "/asr/transcribe",
            estimated_cost=asr_cost(seconds),
            headers={"Accept": "application/json"},
            files={
                "request": (None, json.dumps(settings), "application/json"),
                "audio": ("report.wav", audio, "audio/wav"),
            },
        )
        transcript = payload.get("transcript")
        if not isinstance(transcript, str):
            raise MetaApiError("unexpected transcription response shape")
        billed = float(payload.get("audioDurationMs", seconds * 1000)) / 1000.0
        self._log(
            model=MODEL_TRANSCRIBE,
            endpoint="/asr/transcribe",
            purpose="transcribe",
            audio_seconds=billed,
            est_cost_usd=asr_cost(billed),
        )
        return transcript

    # --- ImagePort ----------------------------------------------------------------

    def generate(self, prompt: str, size: str | None = None) -> bytes:
        body: dict = {"model": MODEL_IMAGE, "prompt": prompt, "n": 1, "output_format": "png", "response_format": "b64_json"}
        if size:
            body["size"] = size
        payload = self._post("/images/generations", json=body, estimated_cost=image_cost(1))
        try:
            image = base64.b64decode(payload["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MetaApiError("unexpected image generation response shape") from exc
        usage = payload.get("usage") or {}
        self._log(
            model=MODEL_IMAGE,
            endpoint="/images/generations",
            purpose="image",
            images=1,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            est_cost_usd=image_cost(1),
        )
        return image

    # --- VideoSegmentationPort ------------------------------------------------------

    def segment_video(self, video: bytes, prompt: str, purpose: str = "segment_video", content_type: str = "video/mp4") -> VideoSegmentation:
        """Segment and track one concept ("water", "red post") through a clip with sam-3.1.

        One noun phrase per request. Masks come back as box-local one_bit rasters and are placed into
        full-frame bit-row masks. Billing is per frame with a 50-frame minimum.
        """
        if len(video) > INLINE_MEDIA_LIMIT_BYTES:
            raise MetaApiError("clip is too large to send inline; upload it with the Files API")
        data_url = f"data:{content_type};base64,{base64.b64encode(video).decode()}"
        # Worst case for the budget check: 15 s at 30 fps.
        return self._run_segmentation(
            "video", {"type": "input_video", "video_url": data_url}, prompt, purpose, sam_cost(frames=450)
        )

    def segment_image(
        self, image: bytes, prompt: str, purpose: str = "segment_image", content_type: str = "image/jpeg"
    ) -> VideoSegmentation:
        """Segment one concept ("water", "staff gauge") in a still picture with sam-3.1 ($2.50 per 1,000 images).

        The result has the same shape as a one-frame clip (frame index 0), so the mask helpers are shared.
        """
        if len(image) > INLINE_MEDIA_LIMIT_BYTES:
            raise MetaApiError("image is too large to send inline")
        data_url = f"data:{content_type};base64,{base64.b64encode(image).decode()}"
        return self._run_segmentation(
            "image", {"type": "input_image", "image_url": data_url}, prompt, purpose, sam_cost(images=1)
        )

    def _run_segmentation(
        self, media: str, media_part: dict, prompt: str, purpose: str, estimated_cost: float
    ) -> VideoSegmentation:
        body = {
            "model": MODEL_SEGMENTATION,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}, media_part],
                }
            ],
            "stream": True,
            "metadata": {"mask_encoding": "one_bit", "include_confidence": "true"},
        }
        deltas: list[str] = []
        usage: Mapping = {}
        completed = False
        for event in self._sse_events("/responses", body, estimated_cost):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                deltas.append(str(event.get("delta", "")))
            elif kind == "response.completed":
                completed = True
                usage = (event.get("response") or {}).get("usage") or {}
            elif kind in ("response.failed", "error"):
                error = (event.get("response") or {}).get("error") or event.get("error") or {}
                message = error.get("message") if isinstance(error, Mapping) else str(error)
                raise MetaApiError(self._redact(f"segmentation failed: {message or 'unknown error'}"))
        if not completed:
            raise MetaApiError("segmentation stream ended before response.completed")
        if media == "image":
            self._log(
                model=MODEL_SEGMENTATION,
                endpoint="/responses",
                purpose=purpose,
                images=1,
                est_cost_usd=sam_cost(images=1),
            )
            return _parse_segmentation(prompt, deltas, None, media="image")
        billed = usage.get("video_frames_processed")
        self._log(
            model=MODEL_SEGMENTATION,
            endpoint="/responses",
            purpose=purpose,
            frames=billed,
            est_cost_usd=sam_cost(frames=int(billed or 0)),
        )
        return _parse_segmentation(prompt, deltas, int(billed) if billed is not None else None)


def _parse_segmentation(
    prompt: str, deltas: Sequence[str], billed_frames: int | None, media: str = "video"
) -> VideoSegmentation:
    from meta_sam_parser import CompletedOutcome, decode_mask_to_raster, image_segmentation_format, video_segmentation_format

    fmt = image_segmentation_format() if media == "image" else video_segmentation_format()
    parser = fmt.create_parser()
    text = ""
    for delta in deltas:
        if _FRAME_LINE.match(delta) and text and not text.endswith("\n"):
            parser.push("\n")
            text += "\n"
        parser.push(delta)
        text += delta
    result = parser.finish(CompletedOutcome()).result
    size = _SIZE.search(result.raw_output or text)
    width, height = (int(size.group(1)), int(size.group(2))) if size else (0, 0)

    boxes: dict[tuple[int, str], object] = {}
    masks: dict[tuple[int, str], object] = {}
    order: list[tuple[int, str]] = []
    for record in result.records:
        if record.frame is None and media != "image":
            continue
        key = (record.frame.frame_index if record.frame is not None else 0, record.object_id)
        if record.kind == "box":
            boxes[key] = record
            if key not in order:
                order.append(key)
        elif record.kind == "mask":
            masks[key] = record

    frames: dict[int, list[TrackedObject]] = {}
    for key in order:
        box, mask_record = boxes[key], masks.get(key)
        if mask_record is None:
            continue
        raster = decode_mask_to_raster(mask_record.mask)
        mask = Mask.from_raster(
            raster,
            mask_record.mask.width,
            mask_record.mask.height,
            left=int(mask_record.bounds.left),
            top=int(mask_record.bounds.top),
            frame_width=width,
            frame_height=height,
        )
        frames.setdefault(key[0], []).append(
            TrackedObject(
                object_id=key[1],
                box=(int(box.left), int(box.top), int(box.right), int(box.bottom)),
                mask=mask,
                confidence=box.confidence,
            )
        )
    ordered = tuple(FrameSegmentation(index, tuple(objs)) for index, objs in sorted(frames.items()))
    return VideoSegmentation(prompt=prompt, width=width, height=height, frames=ordered, billed_frames=billed_frames)


def load_adapter_from_env(
    data_dir: "os.PathLike[str] | str | None" = None,
    env_file: "os.PathLike[str] | str | None" = None,
    budget_usd: float | None = None,
) -> MetaModelApiAdapter:
    """Build the live adapter: load `.env` into the process environment (never printed), read the key,
    log usage to `<data_dir>/derived/meta_usage.jsonl` and enforce the spend budget.
    """
    from pathlib import Path

    from dotenv import load_dotenv

    from willay.adapters.data_dir import resolve_data_dir
    from willay.adapters.meta_usage import DEFAULT_BUDGET_USD

    env_path = Path(env_file) if env_file else Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    config = MetaModelApiConfig.from_env()
    root = Path(data_dir) if data_dir else Path(resolve_data_dir(os.environ))
    limit = budget_usd if budget_usd is not None else float(os.environ.get("WILLAY_META_BUDGET_USD", DEFAULT_BUDGET_USD))
    return MetaModelApiAdapter(config, usage_log=UsageLog(root / "derived" / "meta_usage.jsonl", budget_usd=limit))

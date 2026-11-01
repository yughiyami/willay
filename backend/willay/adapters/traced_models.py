"""Wrappers that record every model call in the active run trace (see application/orchestration/trace.py).

They add nothing to what the wrapped adapter does: same arguments, same results, same errors. When no run is
active they are transparent. Costs are the published-price estimates (the usage log and budget guard live in the
wrapped adapter).
"""

from __future__ import annotations

import time
from typing import Mapping, Sequence

from willay.adapters.meta_usage import asr_cost, image_cost, sam_cost, text_cost
from willay.application.orchestration.trace import current_trace, summarize_args
from willay.domain.segmentation import VideoSegmentation
from willay.ports.agent import AgentTurn

MODEL_SAM = "sam-3.1"
MODEL_IMAGE = "muse-image-1.0"
MODEL_VOICE = "muse-voice-transcribe-1.0"


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class TracedAgentModel:
    def __init__(self, inner, default_model: str = "muse-spark-1.3") -> None:
        self._inner = inner
        self._default_model = default_model

    def __getattr__(self, name: str):  # everything else (usage log, other ports) is the wrapped adapter's
        return getattr(self._inner, name)

    def run_turn(
        self,
        *,
        instructions: str,
        input_items: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]],
        previous_response_id: str | None = None,
        purpose: str = "",
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> AgentTurn:
        chosen = model or self._default_model
        trace = current_trace()
        started = time.perf_counter()
        kwargs: dict = dict(
            instructions=instructions, input_items=input_items, tools=tools, previous_response_id=previous_response_id, purpose=purpose
        )
        if chosen:
            kwargs["model"] = chosen
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens
        try:
            turn = self._inner.run_turn(**kwargs)
        except Exception as exc:
            if trace is not None:
                trace.emit(
                    "model_call", model=chosen, status="error", latency_ms=_ms(started),
                    result_summary=f"{type(exc).__name__}: {str(exc)[:160]}", args_summary=f"purpose={purpose or 'agent'}",
                )
            raise
        if trace is not None:
            used = turn.model or chosen
            trace.emit(
                "model_call",
                model=used,
                latency_ms=_ms(started),
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                cost_usd=text_cost(used, turn.input_tokens, turn.output_tokens, turn.cached_tokens),
                args_summary=f"purpose={purpose or 'agent'} tools={len(tools)}",
                result_summary=("tool_calls: " + ", ".join(c.name for c in turn.tool_calls)) if turn.tool_calls else f"text {len(turn.text)} chars",
            )
        return turn


class TracedSegmenter:
    def __init__(self, inner, model_label: str = MODEL_SAM) -> None:
        self._inner = inner
        self._label = model_label

    def segment_video(self, video: bytes, prompt: str, purpose: str = "") -> VideoSegmentation:
        trace = current_trace()
        started = time.perf_counter()
        try:
            result = self._inner.segment_video(video, prompt, purpose=purpose)
        except Exception as exc:
            if trace is not None:
                trace.emit("model_call", model=self._label, status="error", latency_ms=_ms(started), result_summary=type(exc).__name__)
            raise
        if trace is not None:
            billed = result.billed_frames
            trace.emit(
                "model_call",
                model=self._label,
                latency_ms=_ms(started),
                cost_usd=sam_cost(frames=billed) if billed is not None and self._label == MODEL_SAM else 0.0,
                args_summary=summarize_args({"prompt": prompt, "purpose": purpose}, safe_text_keys=("prompt",)),
                result_summary=f"{len(result.frames)} frames with a match, billed_frames={billed}",
            )
        return result


class TracedImage:
    def __init__(self, inner, model_label: str = MODEL_IMAGE) -> None:
        self._inner = inner
        self._label = model_label

    def generate(self, prompt: str, size: str | None = None) -> bytes:
        trace = current_trace()
        started = time.perf_counter()
        try:
            image = self._inner.generate(prompt, size) if size else self._inner.generate(prompt)
        except Exception as exc:
            if trace is not None:
                trace.emit("model_call", model=self._label, status="error", latency_ms=_ms(started), result_summary=type(exc).__name__)
            raise
        if trace is not None:
            trace.emit(
                "model_call",
                model=self._label,
                latency_ms=_ms(started),
                cost_usd=image_cost(1) if self._label == MODEL_IMAGE else 0.0,
                args_summary=summarize_args({"prompt": prompt, "size": size or ""}),
                result_summary=f"png {len(image)} bytes",
            )
        return image


class TracedTranscriber:
    def __init__(self, inner) -> None:
        self._inner = inner

    def transcribe(self, audio: bytes, language: str = "es") -> str:
        trace = current_trace()
        started = time.perf_counter()
        text = self._inner.transcribe(audio, language)
        if trace is not None:
            # 16 kHz mono 16-bit PCM: 32,000 bytes per second.
            trace.emit(
                "model_call", model=MODEL_VOICE, latency_ms=_ms(started), cost_usd=asr_cost(len(audio) / 32000.0),
                args_summary=f"language={language} audio_bytes={len(audio)}", result_summary=f"transcript {len(text)} chars",
            )
        return text

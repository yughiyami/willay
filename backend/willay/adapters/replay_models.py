"""Recorded model turns for the offline demo.

`RecordingAgentModel` wraps the live adapter once (scripts/record_demo.py) and writes every turn of a real orchestrated
run: which agent asked, which model answered, the text, the tool calls and the token usage. `ReplayAgentModel` plays
those turns back in order, so the real orchestrator, tools, domain rules and guardrails run unchanged with no network
and no cost. The recording contains no prompts, no keys and no personal data; site ids are stored as `$SITE_ID`.
"""

from __future__ import annotations

import base64
import threading
import time
import zlib
from typing import Mapping, Sequence

from willay.domain.segmentation import FrameSegmentation, Mask, TrackedObject, VideoSegmentation
from willay.ports.agent import AgentTurn, ToolCall

SITE_PLACEHOLDER = "$SITE_ID"


class ReplayMismatch(RuntimeError):
    """The run asked for a turn the recording does not have (the code or the data changed since it was recorded)."""


def _swap(value, old: str, new: str):
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, Mapping):
        return {k: _swap(v, old, new) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_swap(v, old, new) for v in value]
    return value


class ReplayAgentModel:
    def __init__(self, recording: Mapping, site_id: str, pace_s: float = 0.6) -> None:
        self._calls = list(recording.get("calls", []))
        self._site_id = site_id
        self._pace = pace_s
        self._index = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        return len(self._calls) - self._index

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
        with self._lock:
            if self._index >= len(self._calls):
                raise ReplayMismatch("the recording has no more turns")
            recorded = self._calls[self._index]
            if purpose and recorded.get("agent") != purpose:
                raise ReplayMismatch(f"the recording expects the {recorded.get('agent')} agent next, not {purpose}")
            self._index += 1
            number = self._index
        if self._pace > 0:
            time.sleep(self._pace)
        calls = tuple(
            ToolCall(str(c.get("call_id") or f"replay-{number}-{i}"), str(c["name"]), _swap(dict(c.get("arguments", {})), SITE_PLACEHOLDER, self._site_id))
            for i, c in enumerate(recorded.get("tool_calls", []))
        )
        return AgentTurn(
            response_id=f"replay-{number}",
            text=_swap(str(recorded.get("text", "")), SITE_PLACEHOLDER, self._site_id),
            tool_calls=calls,
            model=str(recorded.get("model", model or "")),
            input_tokens=int(recorded.get("input_tokens", 0)),
            output_tokens=int(recorded.get("output_tokens", 0)),
            cached_tokens=int(recorded.get("cached_tokens", 0)),
        )


class RecordingAgentModel:
    def __init__(self, inner, site_id: str) -> None:
        self._inner = inner
        self._site_id = site_id
        self._calls: list[dict] = []
        self._lock = threading.Lock()

    def run_turn(self, **kwargs) -> AgentTurn:
        turn = self._inner.run_turn(**kwargs)
        entry = {
            "agent": kwargs.get("purpose", ""),
            "model": turn.model or kwargs.get("model", ""),
            "text": _swap(turn.text, self._site_id, SITE_PLACEHOLDER),
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "cached_tokens": turn.cached_tokens,
            "tool_calls": [
                {"call_id": c.call_id, "name": c.name, "arguments": _swap(dict(c.arguments), self._site_id, SITE_PLACEHOLDER)} for c in turn.tool_calls
            ],
        }
        with self._lock:
            self._calls.append(entry)
        return turn

    def to_dict(self) -> dict:
        with self._lock:
            return {"calls": list(self._calls)}


class ReplayImage:
    """Returns the recorded card for any prompt (the live image model is not called)."""

    def __init__(self, image: bytes) -> None:
        self._image = image

    def generate(self, prompt: str, size: str | None = None) -> bytes:
        return self._image


# --- recorded SAM output ----------------------------------------------------------------------------------------


def _pack_mask(mask: Mask) -> str:
    nbytes = (mask.width + 7) // 8
    raw = b"".join(row.to_bytes(nbytes, "little") for row in mask.rows)
    return base64.b64encode(zlib.compress(raw, 9)).decode()


def _unpack_mask(packed: str, width: int, height: int) -> Mask:
    nbytes = (width + 7) // 8
    raw = zlib.decompress(base64.b64decode(packed))
    return Mask(width, height, tuple(int.from_bytes(raw[y * nbytes : (y + 1) * nbytes], "little") for y in range(height)))


def segmentation_to_dict(segmentation: VideoSegmentation) -> dict:
    return {
        "prompt": segmentation.prompt,
        "width": segmentation.width,
        "height": segmentation.height,
        "billed_frames": segmentation.billed_frames,
        "frames": [
            {
                "index": frame.frame_index,
                "objects": [
                    {"id": o.object_id, "box": list(o.box), "confidence": o.confidence, "mask": _pack_mask(o.mask), "w": o.mask.width, "h": o.mask.height}
                    for o in frame.objects
                ],
            }
            for frame in segmentation.frames
        ],
    }


def segmentation_from_dict(data: Mapping) -> VideoSegmentation:
    return VideoSegmentation(
        prompt=str(data["prompt"]),
        width=int(data["width"]),
        height=int(data["height"]),
        billed_frames=data.get("billed_frames"),
        frames=tuple(
            FrameSegmentation(
                int(f["index"]),
                tuple(
                    TrackedObject(str(o["id"]), tuple(o["box"]), _unpack_mask(o["mask"], int(o["w"]), int(o["h"])), o.get("confidence"))  # type: ignore[arg-type]
                    for o in f["objects"]
                ),
            )
            for f in data["frames"]
        ),
    )


class RecordingSegmenter:
    """Wraps the live SAM adapter and keeps each concept-prompt result so the offline demo can replay it."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._results: dict[str, dict] = {}

    def segment_video(self, video: bytes, prompt: str, purpose: str = "") -> VideoSegmentation:
        result = self._inner.segment_video(video, prompt, purpose=purpose)
        self._results[prompt] = segmentation_to_dict(result)
        return result

    def to_dict(self) -> dict:
        return dict(self._results)


class ReplaySegmenter:
    """Plays back recorded SAM output by prompt, whatever clip bytes are passed (the demo clip is fixed)."""

    def __init__(self, recorded: Mapping) -> None:
        self._recorded = dict(recorded)

    def segment_video(self, video: bytes, prompt: str, purpose: str = "") -> VideoSegmentation:
        if prompt not in self._recorded:
            raise ReplayMismatch(f"no recorded segmentation for the prompt '{prompt}'")
        return segmentation_from_dict(self._recorded[prompt])

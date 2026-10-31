"""Cost estimation, usage log and budget guard for live Meta Model API calls.

Prices are the pay-as-you-go prices published at https://dev.meta.ai/docs/pricing-rate-limits
(checked 2026-10-31). The log stores metadata only (model, endpoint, counts, estimated cost): never
prompts, media, headers or keys.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

# USD per 1M tokens: (cached input, input, output)
_TEXT_RATES: dict[str, tuple[float, float, float]] = {
    "muse-spark-1.3": (0.15, 1.25, 4.25),
    "muse-spark-1.2": (0.15, 1.25, 4.25),
    "muse-spark-1.1": (0.15, 1.25, 4.25),
    "muse-spark-1.3-contributor": (0.002, 0.10, 0.20),
    "muse-spark-1.2-contributor": (0.002, 0.10, 0.20),
    "muse-wonder-1.0": (0.20, 4.00, 20.00),
}
_UNKNOWN_TEXT_RATE = (0.20, 4.00, 20.00)  # priciest known rate: never under-estimate

SAM_VIDEO_PER_FRAME = 0.20 / 1000
SAM_IMAGE_EACH = 2.50 / 1000
SAM_MIN_VIDEO_FRAMES = 50
IMAGE_EACH = 0.01
ASR_PER_HOUR = 0.18

DEFAULT_BUDGET_USD = 3.0

_LOGGED_FIELDS = (
    "model",
    "endpoint",
    "purpose",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "frames",
    "images",
    "audio_seconds",
    "est_cost_usd",
    "status",
)


class BudgetExceededError(RuntimeError):
    """A live call would push the logged spend over the configured budget."""


def text_cost(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    cached_rate, input_rate, output_rate = _TEXT_RATES.get(model, _UNKNOWN_TEXT_RATE)
    cached = min(max(cached_tokens, 0), max(input_tokens, 0))
    fresh = max(input_tokens, 0) - cached
    return (fresh * input_rate + cached * cached_rate + max(output_tokens, 0) * output_rate) / 1_000_000


def sam_cost(frames: int | None = None, images: int | None = None) -> float:
    total = 0.0
    if frames is not None:
        total += max(frames, SAM_MIN_VIDEO_FRAMES) * SAM_VIDEO_PER_FRAME
    if images is not None:
        total += images * SAM_IMAGE_EACH
    return total


def image_cost(n: int = 1) -> float:
    return n * IMAGE_EACH


def asr_cost(seconds: float) -> float:
    return math.floor(max(seconds, 0.0)) * ASR_PER_HOUR / 3600.0


class UsageLog:
    """Append-only JSONL log of live calls with a cumulative budget guard."""

    def __init__(self, path: Path | str, budget_usd: float = DEFAULT_BUDGET_USD) -> None:
        self._path = Path(path)
        self.budget_usd = budget_usd
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, **fields: object) -> None:
        entry: dict[str, object] = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        entry.update({k: fields[k] for k in _LOGGED_FIELDS if k in fields and fields[k] is not None})
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def total_cost(self) -> float:
        if not self._path.exists():
            return 0.0
        total = 0.0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                total += float(json.loads(line).get("est_cost_usd", 0.0))
        return total

    def ensure_within_budget(self, estimated_next_usd: float) -> None:
        spent = self.total_cost()
        if spent + estimated_next_usd > self.budget_usd + 1e-9:
            raise BudgetExceededError(
                f"live-call budget exceeded: spent ${spent:.4f} + next ${estimated_next_usd:.4f} > ${self.budget_usd:.2f}"
            )

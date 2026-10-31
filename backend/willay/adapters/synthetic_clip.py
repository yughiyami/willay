"""Synthetic rising-water clip for dry runs and tests (a fake scene, no people, no real river).

A red post stands in a blue river under a flat sky/bank background. The water line rises by
`rise_cm` over the clip; the scale is `px_per_cm` pixels per centimeter, so the ground truth is
known exactly. This is NOT a model benchmark: it only exercises the pipeline end to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class SyntheticClipSpec:
    width: int = 320
    height: int = 240
    seconds: float = 6.0
    fps: int = 10
    marker_height_cm: float = 100.0  # visible length of the post at the start of the clip
    rise_cm: float = 30.0
    px_per_cm: float = 1.2
    marker_x: tuple[int, int] = (150, 160)
    marker_top: int = 40
    hold_start: float = 0.2  # fraction of the clip with a still river before it starts rising
    hold_end: float = 0.1
    seed: int = 7


def waterline_rows(spec: SyntheticClipSpec, frames: int) -> list[float]:
    start = spec.marker_top + spec.marker_height_cm * spec.px_per_cm
    end = start - spec.rise_cm * spec.px_per_cm
    a, b = int(frames * spec.hold_start), int(frames * (1 - spec.hold_end))
    rows = []
    for i in range(frames):
        t = 0.0 if i <= a else 1.0 if i >= b else (i - a) / max(b - a, 1)
        rows.append(start + (end - start) * t)
    return rows


def render_clip(spec: SyntheticClipSpec) -> tuple[list[np.ndarray], dict]:
    frames_n = int(round(spec.seconds * spec.fps))
    rng = np.random.default_rng(spec.seed)
    rows = waterline_rows(spec, frames_n)
    yy = np.arange(spec.height)[:, None]
    base = np.zeros((spec.height, spec.width, 3), dtype=np.float32)
    base[:] = (165, 155, 135)  # dry bank / background
    base[: spec.height // 4] = (190, 195, 200)  # sky strip
    x1, x2 = spec.marker_x
    out: list[np.ndarray] = []
    for i, row in enumerate(rows):
        img = base.copy()
        img[spec.marker_top : int(row) + 40, x1:x2] = (215, 30, 30)  # post (its lower part is under water)
        water_rows = yy >= int(round(row))
        ripple = 10 * np.sin((yy + i * 2) / 5.0)
        water = np.broadcast_to(np.array([40, 100, 190], dtype=np.float32), img.shape) + ripple[..., None] * np.array([0.3, 0.6, 1.0])
        img = np.where(water_rows[..., None], water, img)
        img += rng.normal(0, 4, img.shape)
        out.append(np.clip(img, 0, 255).astype(np.uint8))
    truth = {
        "frames": frames_n,
        "fps": spec.fps,
        "marker_height_cm": spec.marker_height_cm,
        "rise_cm": spec.rise_cm,
        "px_per_cm": spec.px_per_cm,
        "waterline_rows": [round(r, 2) for r in rows],
        "note": "synthetic scene: not a real river, contains no people",
    }
    return out, truth


def write_mp4(frames: list[np.ndarray], fps: int, path: Path | str) -> None:
    import imageio.v2 as imageio

    with imageio.get_writer(str(path), fps=fps, codec="libx264", pixelformat="yuv420p", quality=8, macro_block_size=None) as writer:
        for frame in frames:
            writer.append_data(frame)

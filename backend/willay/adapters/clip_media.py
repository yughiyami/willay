"""Clip I/O helpers (imageio + ffmpeg), mask conversion, overlays and a colour-threshold dry-run segmenter.

`ColorSegmenter` is NOT a model: it thresholds colours on the synthetic clip so the whole pipeline
(decode, masks, geometry, overlays, HTTP) can be exercised offline and for free.
"""

from __future__ import annotations

import io
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from willay.domain.segmentation import FrameSegmentation, Mask, TrackedObject, VideoSegmentation


@dataclass(frozen=True)
class ClipInfo:
    width: int
    height: int
    fps: float
    frames: int
    duration_s: float


def mask_from_bool(arr: np.ndarray) -> Mask:
    height, width = arr.shape
    packed = np.packbits(arr.astype(np.uint8), axis=1, bitorder="little")
    return Mask(width, height, tuple(int.from_bytes(row.tobytes(), "little") for row in packed))


def bool_from_mask(mask: Mask) -> np.ndarray:
    nbytes = (mask.width + 7) // 8
    out = np.zeros((mask.height, mask.width), dtype=bool)
    for y, row in enumerate(mask.rows):
        if row:
            bits = np.unpackbits(np.frombuffer(row.to_bytes(nbytes, "little"), dtype=np.uint8), bitorder="little")
            out[y] = bits[: mask.width].astype(bool)
    return out


def _with_temp_clip(video: bytes, suffix: str = ".mp4"):
    directory = tempfile.TemporaryDirectory(prefix="willay-clip-")
    path = Path(directory.name) / f"clip{suffix}"
    path.write_bytes(video)
    return directory, path


def probe_clip(video: bytes, suffix: str = ".mp4") -> ClipInfo:
    import imageio.v2 as imageio
    import imageio_ffmpeg

    directory, path = _with_temp_clip(video, suffix)
    with directory:
        frames, seconds = imageio_ffmpeg.count_frames_and_secs(str(path))
        reader = imageio.get_reader(str(path), format="FFMPEG")
        try:
            meta = reader.get_meta_data()
        finally:
            reader.close()
        width, height = meta["size"]
        return ClipInfo(int(width), int(height), float(meta["fps"]), int(frames), float(seconds))


def iter_frames(video: bytes, suffix: str = ".mp4") -> Iterator[tuple[int, np.ndarray]]:
    import imageio.v2 as imageio

    directory, path = _with_temp_clip(video, suffix)
    with directory:
        reader = imageio.get_reader(str(path), format="FFMPEG")
        try:
            for index, frame in enumerate(reader):
                yield index, np.asarray(frame)
        finally:
            reader.close()


def render_overlay(frame_rgb: np.ndarray, water: Mask | None, marker: Mask | None, waterline_row: int | None) -> bytes:
    """PNG with the water (blue) and marker (red) masks blended over the frame and the water line in green."""
    from PIL import Image

    img = frame_rgb.astype(np.float32).copy()
    for mask, color, alpha in ((water, (0, 120, 255), 0.35), (marker, (255, 0, 0), 0.55)):
        if mask is not None:
            sel = bool_from_mask(mask)
            img[sel] = img[sel] * (1 - alpha) + np.array(color, dtype=np.float32) * alpha
    if waterline_row is not None and 0 <= waterline_row < img.shape[0]:
        img[waterline_row : waterline_row + 2, :] = (0, 255, 0)
    buffer = io.BytesIO()
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


class ColorSegmenter:
    """Dry-run stand-in for SAM on the synthetic clip: "water" is blue, anything else is the red post."""

    def segment_video(self, video: bytes, prompt: str, purpose: str = "", content_type: str = "video/mp4") -> VideoSegmentation:
        want_water = "water" in prompt.lower()
        frames: list[FrameSegmentation] = []
        width = height = 0
        total = 0
        for index, frame in iter_frames(video):
            total += 1
            height, width = frame.shape[:2]
            r, g, b = (frame[..., c].astype(np.int16) for c in range(3))
            sel = (b > r + 40) & (b > g + 20) if want_water else (r > 140) & (g < 100) & (b < 100)
            mask = mask_from_bool(sel)
            box = mask.bbox()
            if box is not None and mask.count() >= 20:
                frames.append(FrameSegmentation(index, (TrackedObject("0", box, mask, 0.9),)))
        return VideoSegmentation(prompt=prompt, width=width, height=height, frames=tuple(frames), billed_frames=max(total, 50))

"""Use case: measure the river's rise on a painted marker from a short clip.

Two concept-prompted segmentations (one noun phrase per request, as SAM 3.1 requires): the water and
the marker. The geometry that turns masks into centimeters is deterministic (`domain.water_line`).
The result is a measurement only: alert tiers are decided elsewhere, by domain rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from willay.domain.segmentation import Mask, VideoSegmentation
from willay.domain.water_line import FrameMasks, WaterLineConfig, WatchResult, analyze_water_line
from willay.ports.segmentation import VideoSegmentationPort

DEFAULT_WATER_PROMPT = "water"
DEFAULT_MARKER_PROMPT = "red post"


@dataclass(frozen=True)
class ClipMeasurement:
    watch: WatchResult
    marker_object_id: str | None
    billed_frames: int | None
    frame_masks: tuple[FrameMasks, ...]


def _merge_water(segmentation: VideoSegmentation) -> dict[int, tuple[Mask, float | None]]:
    merged: dict[int, tuple[Mask, float | None]] = {}
    for frame in segmentation.frames:
        union: Mask | None = None
        confidences: list[float] = []
        for tracked in frame.objects:
            union = tracked.mask if union is None else union.union(tracked.mask)
            if tracked.confidence is not None:
                confidences.append(tracked.confidence)
        if union is not None:
            merged[frame.frame_index] = (union, sum(confidences) / len(confidences) if confidences else None)
    return merged


def _pick_marker_object(segmentation: VideoSegmentation, min_aspect: float) -> str | None:
    """The slender object with the largest total area: a post, not a blob."""
    area: dict[str, int] = {}
    for frame in segmentation.frames:
        for tracked in frame.objects:
            box = tracked.mask.bbox()
            if box is None:
                continue
            width, height = box[2] - box[0], box[3] - box[1]
            if height / max(width, 1) >= min_aspect:
                area[tracked.object_id] = area.get(tracked.object_id, 0) + tracked.mask.count()
    return max(area, key=lambda key: (area[key], key)) if area else None


def measure_clip(
    video: bytes,
    segmenter: VideoSegmentationPort,
    marker_height_cm: float,
    *,
    water_prompt: str = DEFAULT_WATER_PROMPT,
    marker_prompt: str = DEFAULT_MARKER_PROMPT,
    total_frames: int | None = None,
    config: WaterLineConfig | None = None,
) -> ClipMeasurement:
    if marker_height_cm <= 0:
        raise ValueError("marker_height_cm must be positive")
    cfg = config or WaterLineConfig()
    water_seg = segmenter.segment_video(video, water_prompt, purpose="watch_water")
    marker_seg = segmenter.segment_video(video, marker_prompt, purpose="watch_marker")

    water = _merge_water(water_seg)
    marker_id = _pick_marker_object(marker_seg, cfg.min_aspect)
    marker_masks: dict[int, tuple[Mask, float | None]] = {}
    if marker_id is not None:
        for frame in marker_seg.frames:
            for tracked in frame.objects:
                if tracked.object_id == marker_id:
                    marker_masks[frame.frame_index] = (tracked.mask, tracked.confidence)

    last_seen = max([*water.keys(), *marker_masks.keys(), -1]) + 1
    count = max(total_frames if total_frames is not None else last_seen, last_seen)
    frames: list[FrameMasks] = []
    for index in range(count):
        water_mask, water_conf = water.get(index, (None, None))
        marker_mask, marker_conf = marker_masks.get(index, (None, None))
        known = [c for c in (water_conf, marker_conf) if c is not None]
        frames.append(FrameMasks(index, water_mask, marker_mask, sum(known) / len(known) if known else None))

    watch = analyze_water_line(frames, marker_height_cm, cfg)
    billed = [b for b in (water_seg.billed_frames, marker_seg.billed_frames) if b is not None]
    return ClipMeasurement(watch, marker_id, sum(billed) if billed else None, tuple(frames))

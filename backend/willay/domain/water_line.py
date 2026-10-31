"""Deterministic river water-line measurement on a painted marker from per-frame masks.

Inputs are plain masks (from any segmenter). The geometry is thresholding only:

1. The marker's pixel length in the first frames (its visible part above the water) is matched to
   the known marker height in centimeters: ``cm_per_px = marker_height_cm / marker_px``.
2. In each frame the water line is the topmost row, at or below the marker top, where the water mask
   covers at least ``min_cover`` of the marker columns (plus a margin on both sides) for
   ``persistence_rows`` consecutive rows. Isolated noise rows do not qualify.
3. ``rise_cm`` is the displacement of the water line from its starting position (median of the last
   ``tail_fraction`` of the valid frames).

No model decides anything here, and no alert tier is produced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from willay.domain.segmentation import Mask


@dataclass(frozen=True)
class WaterLineConfig:
    ref_frames: int = 5
    min_cover: float = 0.5
    persistence_rows: int = 3
    column_margin_factor: float = 1.0
    smooth_window: int = 5
    min_valid_fraction: float = 0.5
    min_marker_px: int = 20
    min_aspect: float = 2.0
    tail_fraction: float = 0.1
    noise_floor_marker_fraction: float = 0.02
    marker_jitter_marker_fraction: float = 0.03


@dataclass(frozen=True)
class FrameMasks:
    frame_index: int
    water: Mask | None
    marker: Mask | None
    confidence: float | None = None


@dataclass(frozen=True)
class WaterLinePoint:
    frame_index: int
    valid: bool
    waterline_row: int | None = None
    visible_cm: float | None = None  # marker length still above the water
    raw_rise_cm: float | None = None
    rise_cm: float | None = None  # median-smoothed


@dataclass(frozen=True)
class WatchResult:
    rise_cm: float | None
    confidence: float
    cm_per_px: float | None
    marker_px: float | None
    valid_frames: int
    total_frames: int
    points: tuple[WaterLinePoint, ...]
    reasons: tuple[str, ...]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _rolling_median(values: list[float], window: int) -> list[float]:
    half = max(window, 1) // 2
    return [_median(values[max(0, i - half) : i + half + 1]) for i in range(len(values))]


def _marker_geometry(marker: Mask | None, cfg: WaterLineConfig) -> tuple[int, int, int, int] | None:
    if marker is None:
        return None
    box = marker.bbox()
    if box is None:
        return None
    x1, y1, x2, y2 = box
    height, width = y2 - y1, x2 - x1
    if height < cfg.min_marker_px or height / max(width, 1) < cfg.min_aspect:
        return None
    return box


def _waterline_row(water: Mask | None, columns: tuple[int, int], top: int, cfg: WaterLineConfig) -> int | None:
    if water is None:
        return None
    x1, x2 = columns
    need = cfg.min_cover * (x2 - x1)
    run = 0
    for y in range(max(top, 0), water.height):
        if water.row_count(y, x1, x2) >= need:
            run += 1
            if run >= cfg.persistence_rows:
                return y - cfg.persistence_rows + 1
        else:
            run = 0
    return None


def _failure(total: int, reasons: tuple[str, ...], cm_per_px: float | None = None, marker_px: float | None = None,
             valid: int = 0, points: tuple[WaterLinePoint, ...] = ()) -> WatchResult:
    return WatchResult(None, 0.0, cm_per_px, marker_px, valid, total, points, reasons)


def analyze_water_line(
    frames: list[FrameMasks] | tuple[FrameMasks, ...], marker_height_cm: float, config: WaterLineConfig | None = None
) -> WatchResult:
    if marker_height_cm <= 0:
        raise ValueError("marker_height_cm must be positive")
    cfg = config or WaterLineConfig()
    ordered = sorted(frames, key=lambda f: f.frame_index)
    total = len(ordered)

    geometries = [(f.frame_index, g) for f in ordered if (g := _marker_geometry(f.marker, cfg)) is not None]
    if not geometries:
        return _failure(total, ("marker_not_found",))

    ref = [g for _, g in geometries[: cfg.ref_frames]]
    marker_top = _median([g[1] for g in ref])
    marker_px = _median([g[3] - g[1] for g in ref])
    cx1 = _median([g[0] for g in ref])
    cx2 = _median([g[2] for g in ref])
    margin = (cx2 - cx1) * cfg.column_margin_factor
    columns = (int(math.floor(cx1 - margin)), int(math.ceil(cx2 + margin)))
    cm_per_px = marker_height_cm / marker_px

    rows: dict[int, int] = {}
    for f in ordered:
        row = _waterline_row(f.water, columns, int(marker_top), cfg)
        if row is not None:
            rows[f.frame_index] = row
    valid_fraction = len(rows) / total if total else 0.0
    if len(rows) <= cfg.ref_frames or valid_fraction < cfg.min_valid_fraction:
        points = tuple(WaterLinePoint(f.frame_index, False) for f in ordered)
        return _failure(total, ("too_few_valid_frames",), cm_per_px, marker_px, len(rows), points)

    valid_indices = [f.frame_index for f in ordered if f.frame_index in rows]
    reference_row = _median([float(rows[i]) for i in valid_indices[: cfg.ref_frames]])
    raw = [(reference_row - rows[i]) * cm_per_px for i in valid_indices]
    smooth = _rolling_median(raw, cfg.smooth_window)
    by_index = {i: (r, s) for i, r, s in zip(valid_indices, raw, smooth)}

    points = tuple(
        WaterLinePoint(
            f.frame_index,
            True,
            rows[f.frame_index],
            (rows[f.frame_index] - marker_top) * cm_per_px,
            by_index[f.frame_index][0],
            by_index[f.frame_index][1],
        )
        if f.frame_index in rows
        else WaterLinePoint(f.frame_index, False)
        for f in ordered
    )

    tail = max(1, math.ceil(cfg.tail_fraction * len(smooth)))
    rise_cm = _median(raw[-tail:])  # robust to outliers; the smoothed series is for display only

    residual_px = _median([abs(r - s) for r, s in zip(raw, smooth)]) / cm_per_px
    smoothness = 1.0 - min(1.0, residual_px / (cfg.noise_floor_marker_fraction * marker_px))
    tops = [float(g[1]) for _, g in geometries]
    jitter_px = _median([abs(t - _median(tops)) for t in tops])
    stability = 1.0 - min(1.0, jitter_px / (cfg.marker_jitter_marker_fraction * marker_px))
    confidences = [f.confidence for f in ordered if f.frame_index in rows and f.confidence is not None]
    segmenter_conf = sum(confidences) / len(confidences) if confidences else 1.0
    confidence = valid_fraction * smoothness * stability * max(0.0, min(1.0, segmenter_conf))

    reasons: list[str] = []
    if valid_fraction < 0.8:
        reasons.append("many_frames_without_a_water_line")
    if stability < 0.6:
        reasons.append("marker_moves_between_frames")
    return WatchResult(rise_cm, round(confidence, 4), cm_per_px, marker_px, len(rows), total, points, tuple(reasons))

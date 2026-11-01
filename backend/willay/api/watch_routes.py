"""POST /watch/clip: river-watcher clip in, deterministic water-rise measurement out.

POST /watch/demo-clip analyses the bundled FAKE rising-water clip offline (recorded SAM output when a recording exists,
otherwise the colour-threshold stand-in) so the console can show the watcher without any network call.
"""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from willay.adapters.clip_media import ColorSegmenter, iter_frames, probe_clip, render_overlay
from willay.adapters.meta_usage import SAM_VIDEO_PER_FRAME
from willay.api.dependencies import AppDependencies
from willay.application.watch_clip import DEFAULT_MARKER_PROMPT, DEFAULT_WATER_PROMPT, ClipMeasurement, measure_clip

MAX_CLIP_SECONDS = 15.0
MAX_CLIP_BYTES = 40_000_000
OVERLAY_COUNT = 3


def _pick_overlay_frames(valid_frames: list[int]) -> list[int]:
    if not valid_frames:
        return []
    picks = [valid_frames[0], valid_frames[len(valid_frames) // 2], valid_frames[-1]]
    return list(dict.fromkeys(picks))


def render_overlays(video: bytes, suffix: str, measurement) -> list[tuple[int, bytes]]:
    """Up to three annotated frames (first, middle, last valid) with the water mask, marker mask and water line."""
    by_index = {m.frame_index: m for m in measurement.frame_masks}
    rows = {p.frame_index: p.waterline_row for p in measurement.watch.points if p.valid}
    wanted = set(_pick_overlay_frames(sorted(rows)))
    out: list[tuple[int, bytes]] = []
    if not wanted:
        return out
    for index, frame in iter_frames(video, suffix):
        if index in wanted:
            masks = by_index.get(index)
            out.append((index, render_overlay(frame, masks.water if masks else None, masks.marker if masks else None, rows.get(index))))
        if len(out) == len(wanted):
            break
    return out


def watch_payload(
    video: bytes, suffix: str, info, measurement: ClipMeasurement, marker_height_cm: float, water: str, marker: str, **extra: object
) -> dict:
    watch = measurement.watch
    overlays = [{"frame": index, "png_base64": base64.b64encode(png).decode()} for index, png in render_overlays(video, suffix, measurement)]
    billed = measurement.billed_frames
    rise = None if watch.rise_cm is None else round(watch.rise_cm, 1)
    return {
        "rise_cm": rise,
        "confidence": watch.confidence,
        "cm_per_px": None if watch.cm_per_px is None else round(watch.cm_per_px, 4),
        "marker_px": watch.marker_px,
        "marker_height_cm": marker_height_cm,
        "valid_frames": watch.valid_frames,
        "total_frames": watch.total_frames,
        "reasons": list(watch.reasons),
        "clip": {"seconds": round(info.duration_s, 2), "fps": info.fps, "width": info.width, "height": info.height},
        "prompts": {"water": water, "marker": marker},
        "series": [
            {
                "frame": p.frame_index,
                "valid": p.valid,
                "rise_cm": None if p.rise_cm is None else round(p.rise_cm, 1),
                "visible_cm": None if p.visible_cm is None else round(p.visible_cm, 1),
            }
            for p in watch.points
        ],
        "overlays": overlays,
        "billed_frames": billed,
        "estimated_cost_usd": None if billed is None else round(billed * SAM_VIDEO_PER_FRAME, 6),
        "tier_input": {"water_rise_cm": rise},
        "note": (
            "Deterministic geometry on SAM masks: a measurement, not an alert. Tiers come only from the domain rules "
            "(/alerts/evaluate); this value can be passed as water_rise_cm."
        ),
        **extra,
    }


def register_watch_routes(app: FastAPI, deps: AppDependencies) -> None:
    @app.post("/watch/clip")
    def watch_clip(
        file: Annotated[UploadFile, File(description="MP4 or MOV, at most 15 s, no people in frame")],
        marker_height_cm: Annotated[float, Form(gt=0, description="Visible length of the marker at the start of the clip, in cm")],
        no_people_confirmed: Annotated[bool, Form(description="Must be true: clips are sent to a third-party model")] = False,
        water_prompt: Annotated[str | None, Form()] = None,
        marker_prompt: Annotated[str | None, Form()] = None,
        register_clip: Annotated[bool, Form(description="Also keep the clip so the Watcher agent can measure it")] = False,
    ) -> dict:
        if deps.video_segmenter is None:
            raise HTTPException(status_code=503, detail="the segmentation model is not configured (set WILLAY_META_ENABLED=1 and META_API_KEY)")
        if not no_people_confirmed:
            raise HTTPException(status_code=400, detail="confirm that the clip shows no people (no_people_confirmed=true): clips are sent to a third-party model")
        content_type = (file.content_type or "").lower()
        if content_type not in ("video/mp4", "video/quicktime"):
            raise HTTPException(status_code=415, detail="only MP4 or MOV clips are accepted")
        video = file.file.read(MAX_CLIP_BYTES + 1)
        if len(video) > MAX_CLIP_BYTES:
            raise HTTPException(status_code=413, detail="clip is larger than 40 MB")
        suffix = ".mov" if content_type == "video/quicktime" else ".mp4"
        try:
            info = probe_clip(video, suffix)
        except Exception as exc:  # decoder errors are heterogeneous (OSError, RuntimeError, ValueError...)
            raise HTTPException(status_code=422, detail="could not decode the clip as MP4/MOV video") from exc
        if info.frames <= 0 or info.duration_s <= 0:
            raise HTTPException(status_code=422, detail="the clip has no decodable frames")
        if info.duration_s > MAX_CLIP_SECONDS + 0.25:
            raise HTTPException(status_code=422, detail=f"clip is {info.duration_s:.1f} s long; the limit is {MAX_CLIP_SECONDS:.0f} s")

        water = (water_prompt or DEFAULT_WATER_PROMPT).strip()
        marker = (marker_prompt or DEFAULT_MARKER_PROMPT).strip()
        measurement = measure_clip(
            video, deps.video_segmenter, marker_height_cm, water_prompt=water, marker_prompt=marker, total_frames=info.frames
        )
        extra: dict = {"analyzer": "sam-3.1"}
        if register_clip:
            extra["clip_id"] = deps.clips.add(f"clip-{len(deps.clips.ids()) + 1}", video, content_type)
        return watch_payload(video, suffix, info, measurement, marker_height_cm, water, marker, **extra)

    @app.post("/watch/demo-clip")
    def watch_demo_clip() -> dict:
        from willay.adapters.replay_models import ReplaySegmenter
        from willay.application.orchestration.demo_script import DEMO_MARKER_CM, DEMO_WATER_PROMPT
        from willay.api.demo_routes import demo_clip_bytes

        video = demo_clip_bytes()
        info = probe_clip(video, ".mp4")
        recorded = (deps.demo_recording or {}).get("segmentations")
        segmenter = ReplaySegmenter(recorded) if recorded else ColorSegmenter()
        analyzer = "offline: recorded SAM 3.1 output on the fake clip" if recorded else "offline: colour-threshold stand-in (not SAM)"
        measurement = measure_clip(
            video, segmenter, DEMO_MARKER_CM, water_prompt=DEMO_WATER_PROMPT, marker_prompt=DEFAULT_MARKER_PROMPT, total_frames=info.frames
        )
        return watch_payload(video, ".mp4", info, measurement, DEMO_MARKER_CM, DEMO_WATER_PROMPT, DEFAULT_MARKER_PROMPT, analyzer=analyzer, synthetic_clip=True, estimated_cost_usd=0.0)

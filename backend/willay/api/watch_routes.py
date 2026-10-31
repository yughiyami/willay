"""POST /watch/clip: river-watcher clip in, deterministic water-rise measurement out."""

from __future__ import annotations

import base64
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from willay.adapters.clip_media import iter_frames, probe_clip, render_overlay
from willay.adapters.meta_usage import SAM_VIDEO_PER_FRAME
from willay.api.dependencies import AppDependencies
from willay.application.watch_clip import DEFAULT_MARKER_PROMPT, DEFAULT_WATER_PROMPT, measure_clip

MAX_CLIP_SECONDS = 15.0
MAX_CLIP_BYTES = 40_000_000
OVERLAY_COUNT = 3


def _pick_overlay_frames(valid_frames: list[int]) -> list[int]:
    if not valid_frames:
        return []
    picks = [valid_frames[0], valid_frames[len(valid_frames) // 2], valid_frames[-1]]
    return list(dict.fromkeys(picks))


def register_watch_routes(app: FastAPI, deps: AppDependencies) -> None:
    @app.post("/watch/clip")
    def watch_clip(
        file: Annotated[UploadFile, File(description="MP4 or MOV, at most 15 s, no people in frame")],
        marker_height_cm: Annotated[float, Form(gt=0, description="Visible length of the marker at the start of the clip, in cm")],
        no_people_confirmed: Annotated[bool, Form(description="Must be true: clips are sent to a third-party model")] = False,
        water_prompt: Annotated[str | None, Form()] = None,
        marker_prompt: Annotated[str | None, Form()] = None,
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
        watch = measurement.watch
        by_index = {m.frame_index: m for m in measurement.frame_masks}
        rows = {p.frame_index: p.waterline_row for p in watch.points if p.valid}
        wanted = set(_pick_overlay_frames(sorted(rows)))
        overlays = []
        if wanted:
            for index, frame in iter_frames(video, suffix):
                if index in wanted:
                    masks = by_index.get(index)
                    png = render_overlay(frame, masks.water if masks else None, masks.marker if masks else None, rows.get(index))
                    overlays.append({"frame": index, "png_base64": base64.b64encode(png).decode()})
                if len(overlays) == len(wanted):
                    break

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
        }

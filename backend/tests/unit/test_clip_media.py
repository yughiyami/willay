"""Clip I/O helpers, the synthetic rising-water clip and the colour-threshold dry-run segmenter."""

import numpy as np
import pytest

from willay.adapters.clip_media import (
    ColorSegmenter,
    bool_from_mask,
    mask_from_bool,
    probe_clip,
    render_overlay,
)
from willay.adapters.synthetic_clip import SyntheticClipSpec, render_clip, write_mp4
from willay.application.watch_clip import measure_clip
from willay.domain.segmentation import Mask


def test_mask_bool_round_trip():
    arr = np.zeros((6, 9), dtype=bool)
    arr[1:4, 2:7] = True
    arr[5, 8] = True
    mask = mask_from_bool(arr)
    assert (mask.width, mask.height) == (9, 6)
    assert mask.count() == arr.sum()
    assert mask.bbox() == (2, 1, 9, 6)
    assert np.array_equal(bool_from_mask(mask), arr)


def test_overlay_is_a_png_with_the_waterline_drawn():
    frame = np.full((40, 60, 3), 200, dtype=np.uint8)
    water = Mask.rectangle(60, 40, 0, 25, 60, 40)
    marker = Mask.rectangle(60, 40, 30, 5, 34, 25)
    png = render_overlay(frame, water, marker, waterline_row=25)
    assert png.startswith(b"\x89PNG")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    spec = SyntheticClipSpec(seconds=6, fps=10, marker_height_cm=100.0, rise_cm=30.0)
    frames, truth = render_clip(spec)
    path = tmp_path_factory.mktemp("clip") / "rising.mp4"
    write_mp4(frames, spec.fps, path)
    return path, spec, truth


def test_synthetic_clip_has_at_least_the_minimum_billable_frames(clip):
    path, spec, truth = clip
    assert truth["frames"] >= 50
    assert truth["rise_cm"] == 30.0
    assert truth["marker_height_cm"] == 100.0
    assert truth["waterline_rows"][0] > truth["waterline_rows"][-1]  # the water goes up the image


def test_probe_reads_size_fps_and_frame_count(clip):
    path, spec, truth = clip
    info = probe_clip(path.read_bytes())
    assert (info.width, info.height) == (spec.width, spec.height)
    assert info.fps == pytest.approx(spec.fps)
    assert info.frames == truth["frames"]
    assert info.duration_s == pytest.approx(truth["frames"] / spec.fps, abs=0.2)


def test_dry_run_end_to_end_recovers_the_rise_within_five_centimeters(clip):
    path, spec, truth = clip
    video = path.read_bytes()
    info = probe_clip(video)
    result = measure_clip(video, ColorSegmenter(), marker_height_cm=spec.marker_height_cm, total_frames=info.frames)
    assert result.watch.rise_cm is not None
    assert abs(result.watch.rise_cm - spec.rise_cm) <= 5.0
    assert result.watch.confidence > 0.6
    assert result.watch.valid_frames >= 0.9 * info.frames


def test_color_segmenter_returns_one_object_per_frame_with_boxes(clip):
    path, _, truth = clip
    seg = ColorSegmenter().segment_video(path.read_bytes(), "water")
    assert len(seg.frames) == truth["frames"]
    assert (seg.width, seg.height) == (320, 240)
    first = seg.frames[0].objects[0]
    assert first.mask.count() > 1000

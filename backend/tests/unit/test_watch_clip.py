"""Use case: measure the water rise on a marker from two concept-prompted segmentations."""

import pytest

from willay.application.watch_clip import ClipMeasurement, measure_clip
from willay.domain.segmentation import FrameSegmentation, Mask, TrackedObject, VideoSegmentation

W, H = 200, 150


def obj(object_id: str, mask: Mask, confidence: float = 0.9) -> TrackedObject:
    return TrackedObject(object_id, mask.bbox() or (0, 0, 0, 0), mask, confidence)


class FakeSegmenter:
    """Returns pre-baked segmentations per prompt and records the prompts it received."""

    def __init__(self, by_prompt: dict[str, VideoSegmentation]) -> None:
        self.by_prompt = by_prompt
        self.calls: list[tuple[str, str]] = []

    def segment_video(self, video: bytes, prompt: str, purpose: str = "") -> VideoSegmentation:
        self.calls.append((prompt, purpose))
        return self.by_prompt[prompt]


def scene(n: int = 30, start: int = 100, end: int = 70, extra_marker_blobs: bool = False):
    water_frames, marker_frames = [], []
    for i in range(n):
        top = start if i < 5 else round(start + (end - start) * (i - 4) / (n - 5))
        water_frames.append(FrameSegmentation(i, (obj("0", Mask.rectangle(W, H, 0, top, W, H)),)))
        post = obj("3", Mask.rectangle(W, H, 90, 10, 100, top), 0.95)
        extras = (obj("7", Mask.rectangle(W, H, 20, 40, 24, 50), 0.99),) if extra_marker_blobs else ()
        marker_frames.append(FrameSegmentation(i, extras + (post,)))
    return (
        VideoSegmentation("water", W, H, tuple(water_frames), billed_frames=n),
        VideoSegmentation("red post", W, H, tuple(marker_frames), billed_frames=n),
    )


def test_measures_the_rise_using_two_prompts_one_concept_each():
    water, marker = scene()
    seg = FakeSegmenter({"water": water, "red post": marker})
    result = measure_clip(b"clip", seg, marker_height_cm=90.0, water_prompt="water", marker_prompt="red post")
    assert isinstance(result, ClipMeasurement)
    assert [p for p, _ in seg.calls] == ["water", "red post"]  # one noun phrase per request
    assert result.watch.rise_cm == pytest.approx(30.0, abs=1.5)
    assert result.watch.confidence > 0.7
    assert result.marker_object_id == "3"
    assert result.billed_frames == 60  # 30 + 30


def test_picks_the_slender_marker_object_not_the_biggest_blob():
    water, marker = scene(extra_marker_blobs=True)
    seg = FakeSegmenter({"water": water, "red post": marker})
    result = measure_clip(b"clip", seg, marker_height_cm=90.0)
    assert result.marker_object_id == "3"
    assert result.watch.rise_cm == pytest.approx(30.0, abs=1.5)


def test_total_frames_includes_frames_the_model_returned_nothing_for():
    water, marker = scene(n=30)
    result = measure_clip(b"clip", FakeSegmenter({"water": water, "red post": marker}), marker_height_cm=90.0, total_frames=60)
    assert result.watch.total_frames == 60
    assert result.watch.valid_frames == 30  # the last 30 frames had no match


def test_no_marker_match_reports_no_measurement():
    water, _ = scene()
    empty = VideoSegmentation("red post", W, H, (), billed_frames=50)
    result = measure_clip(b"clip", FakeSegmenter({"water": water, "red post": empty}), marker_height_cm=90.0)
    assert result.watch.rise_cm is None
    assert "marker_not_found" in result.watch.reasons
    assert result.marker_object_id is None


def test_water_instances_are_merged_per_frame():
    _, marker = scene()
    left = Mask.rectangle(W, H, 0, 100, 95, H)
    right = Mask.rectangle(W, H, 95, 100, W, H)
    water = VideoSegmentation("water", W, H, tuple(FrameSegmentation(i, (obj("0", left), obj("1", right))) for i in range(30)), 30)
    result = measure_clip(b"clip", FakeSegmenter({"water": water, "red post": marker}), marker_height_cm=90.0)
    assert result.watch.valid_frames > 0
    assert result.frame_masks[0].water.count() == (95 + (W - 95)) * (H - 100)


def test_rejects_non_positive_marker_height():
    water, marker = scene()
    with pytest.raises(ValueError):
        measure_clip(b"clip", FakeSegmenter({"water": water, "red post": marker}), marker_height_cm=0)

"""Deterministic water-line geometry on synthetic masks (no model involved)."""

import pytest

from willay.domain.segmentation import Mask
from willay.domain.water_line import FrameMasks, WaterLineConfig, analyze_water_line

W, H = 200, 150
MX1, MX2 = 90, 100  # marker columns (half-open)
MARKER_TOP = 10


def frame(index: int, water_top: int | None, *, marker_bottom: int | None = None, marker=True, water=True,
          noise_rows: tuple[int, ...] = (), confidence: float | None = 0.95) -> FrameMasks:
    """Marker post above the waterline, water below. Rows are half-open."""
    if water_top is None:
        water_top = H
    water_mask = Mask.rectangle(W, H, 0, water_top, W, H) if water else None
    if water_mask is not None:
        for row in noise_rows:  # stray single-row blobs far above the real waterline
            water_mask = water_mask.union(Mask.rectangle(W, H, MX1 - 12, row, MX2 + 12, row + 1))
    bottom = water_top if marker_bottom is None else marker_bottom
    marker_mask = Mask.rectangle(W, H, MX1, MARKER_TOP, MX2, bottom) if marker else None
    return FrameMasks(frame_index=index, water=water_mask, marker=marker_mask, confidence=confidence)


def rising_clip(start: int = 100, end: int = 70, n: int = 30, hold: int = 5, **kw) -> list[FrameMasks]:
    frames = [frame(i, start, **kw) for i in range(hold)]
    steps = n - hold
    for k in range(steps):
        top = round(start + (end - start) * (k + 1) / steps)
        frames.append(frame(hold + k, top, **kw))
    return frames


def test_rise_is_the_waterline_displacement_in_centimeters():
    # Marker is 90 px tall at the start (rows 10..100) and 90 cm long: 1 cm per pixel.
    result = analyze_water_line(rising_clip(100, 70), marker_height_cm=90.0)
    assert result.cm_per_px == pytest.approx(1.0)
    assert result.rise_cm == pytest.approx(30.0, abs=1.5)
    assert result.confidence > 0.8
    assert result.reasons == ()


def test_scale_follows_the_known_marker_height():
    result = analyze_water_line(rising_clip(100, 70), marker_height_cm=45.0)  # 0.5 cm per pixel
    assert result.cm_per_px == pytest.approx(0.5)
    assert result.rise_cm == pytest.approx(15.0, abs=1.0)


def test_flat_water_reports_no_rise_with_high_confidence():
    result = analyze_water_line([frame(i, 100) for i in range(30)], marker_height_cm=90.0)
    assert result.rise_cm == pytest.approx(0.0, abs=0.5)
    assert result.confidence > 0.8


def test_per_frame_series_has_the_level_of_every_frame():
    result = analyze_water_line(rising_clip(100, 70, n=20), marker_height_cm=90.0)
    assert len(result.points) == 20
    first, last = result.points[0], result.points[-1]
    assert first.rise_cm == pytest.approx(0.0, abs=0.5)
    assert last.raw_rise_cm == pytest.approx(30.0, abs=1.0)
    assert last.rise_cm == pytest.approx(30.0, abs=3.0)  # median smoothing lags a ramp at its end
    assert last.visible_cm == pytest.approx(60.0, abs=1.5)  # marker part still above water
    assert all(p.valid for p in result.points)


def test_isolated_noise_rows_do_not_move_the_waterline():
    result = analyze_water_line(rising_clip(100, 70, noise_rows=(30,)), marker_height_cm=90.0)
    assert result.rise_cm == pytest.approx(30.0, abs=1.5)


def test_missing_water_frames_lower_confidence_but_still_measure():
    clean = analyze_water_line(rising_clip(100, 70), marker_height_cm=90.0)
    frames = rising_clip(100, 70)
    for i in (8, 9, 10, 11, 12, 13, 14, 15):
        frames[i] = frame(i, 80, water=False)
    degraded = analyze_water_line(frames, marker_height_cm=90.0)
    assert degraded.rise_cm == pytest.approx(30.0, abs=1.5)
    assert degraded.confidence < clean.confidence
    assert sum(1 for p in degraded.points if not p.valid) == 8


def test_no_marker_anywhere_gives_no_measurement():
    frames = [frame(i, 100, marker=False) for i in range(20)]
    result = analyze_water_line(frames, marker_height_cm=90.0)
    assert result.rise_cm is None and result.confidence == 0.0
    assert "marker_not_found" in result.reasons


def test_too_few_valid_frames_gives_no_measurement():
    frames = [frame(i, 100, water=False) for i in range(18)] + [frame(18, 100), frame(19, 70)]
    result = analyze_water_line(frames, marker_height_cm=90.0)
    assert result.rise_cm is None
    assert "too_few_valid_frames" in result.reasons


def test_a_tiny_or_wide_marker_is_rejected():
    tiny = [FrameMasks(i, Mask.rectangle(W, H, 0, 100, W, H), Mask.rectangle(W, H, 90, 90, 100, 100), 0.9) for i in range(20)]
    result = analyze_water_line(tiny, marker_height_cm=90.0)
    assert result.rise_cm is None and "marker_not_found" in result.reasons


def test_marker_height_must_be_positive():
    with pytest.raises(ValueError):
        analyze_water_line(rising_clip(), marker_height_cm=0)


def test_falling_water_is_reported_as_negative_rise():
    result = analyze_water_line(rising_clip(70, 100), marker_height_cm=60.0)
    assert result.rise_cm is not None and result.rise_cm < 0


def test_lower_sam_confidence_lowers_the_result_confidence():
    high = analyze_water_line(rising_clip(confidence=0.95), marker_height_cm=90.0)
    low = analyze_water_line(rising_clip(confidence=0.40), marker_height_cm=90.0)
    assert low.confidence < high.confidence


def test_config_is_respected():
    cfg = WaterLineConfig(min_valid_fraction=0.99)
    frames = rising_clip()
    frames[3] = frame(3, 100, water=False)
    result = analyze_water_line(frames, marker_height_cm=90.0, config=cfg)
    assert result.rise_cm is None and "too_few_valid_frames" in result.reasons

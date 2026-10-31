"""POST /watch/clip: river-watcher clip upload, measured with an injected segmenter (no network)."""

import base64

import pytest
from fastapi.testclient import TestClient

from willay.adapters.clip_media import ColorSegmenter
from willay.adapters.synthetic_clip import SyntheticClipSpec, render_clip, write_mp4
from willay.api.app import create_app
from willay.api.dependencies import synthetic_dependencies


@pytest.fixture(scope="module")
def clip_bytes(tmp_path_factory):
    spec = SyntheticClipSpec()
    frames, _ = render_clip(spec)
    path = tmp_path_factory.mktemp("clips") / "ok.mp4"
    write_mp4(frames, spec.fps, path)
    return path.read_bytes()


@pytest.fixture(scope="module")
def long_clip_bytes(tmp_path_factory):
    spec = SyntheticClipSpec(width=64, height=48, seconds=16, fps=2, marker_x=(30, 33), marker_top=8, px_per_cm=0.2)
    frames, _ = render_clip(spec)
    path = tmp_path_factory.mktemp("clips") / "long.mp4"
    write_mp4(frames, spec.fps, path)
    return path.read_bytes()


@pytest.fixture()
def client():
    deps = synthetic_dependencies()
    deps.video_segmenter = ColorSegmenter()
    return TestClient(create_app(deps))


def post(client, content, *, height="100", confirmed="true", name="rising.mp4", ctype="video/mp4", **extra):
    return client.post(
        "/watch/clip",
        files={"file": (name, content, ctype)},
        data={"marker_height_cm": height, "no_people_confirmed": confirmed, **extra},
    )


def test_measures_the_rise_and_returns_series_overlays_and_cost(client, clip_bytes):
    response = post(client, clip_bytes)
    assert response.status_code == 200
    body = response.json()
    assert body["rise_cm"] == pytest.approx(30.0, abs=5.0)
    assert 0.5 < body["confidence"] <= 1.0
    assert body["marker_height_cm"] == 100.0
    assert body["total_frames"] == 60 and body["valid_frames"] >= 54
    assert len(body["series"]) == 60
    assert {"frame", "rise_cm", "visible_cm", "valid"} <= set(body["series"][0])
    assert len(body["overlays"]) == 3
    assert base64.b64decode(body["overlays"][0]["png_base64"]).startswith(b"\x89PNG")
    assert body["billed_frames"] >= 100  # two concept requests of >= 50 frames each
    assert body["estimated_cost_usd"] == pytest.approx(body["billed_frames"] * 0.0002)
    assert body["prompts"] == {"water": "water", "marker": "red post"}


def test_the_measurement_never_decides_an_alert_tier(client, clip_bytes):
    body = post(client, clip_bytes).json()
    assert "tier" not in body
    assert body["tier_input"] == {"water_rise_cm": body["rise_cm"]}
    assert "deterministic" in body["note"].lower()


def test_prompts_are_configurable(client, clip_bytes):
    body = post(client, clip_bytes, water_prompt="river water", marker_prompt="red post").json()
    assert body["prompts"]["water"] == "river water"


def test_requires_confirmation_that_no_people_are_in_the_clip(client, clip_bytes):
    assert post(client, clip_bytes, confirmed="false").status_code == 400
    response = client.post("/watch/clip", files={"file": ("a.mp4", clip_bytes, "video/mp4")}, data={"marker_height_cm": "100"})
    assert response.status_code == 400


def test_rejects_non_positive_marker_height(client, clip_bytes):
    assert post(client, clip_bytes, height="0").status_code == 422


def test_rejects_non_video_uploads(client):
    assert post(client, b"not a video", name="x.png", ctype="image/png").status_code == 415


def test_rejects_clips_longer_than_fifteen_seconds(client, long_clip_bytes):
    response = post(client, long_clip_bytes)
    assert response.status_code == 422
    assert "15" in response.json()["detail"]


def test_rejects_undecodable_video(client):
    assert post(client, b"\x00\x00\x00\x18ftypmp42garbage").status_code == 422


def test_unavailable_without_a_configured_segmenter(clip_bytes):
    client = TestClient(create_app(synthetic_dependencies()))
    assert post(client, clip_bytes).status_code == 503

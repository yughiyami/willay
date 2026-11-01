"""Unit tests for the Meta Model API adapter against the documented contract (docs/META_API.md).

A mocked HTTP transport is used: no network.
"""

import base64
import io
import json
import struct

import httpx
import pytest

from willay.adapters.meta_model_api import (
    DEFAULT_BASE_URL,
    MODEL_IMAGE,
    MODEL_SEGMENTATION,
    MODEL_TEXT,
    MODEL_TRANSCRIBE,
    MetaApiError,
    MetaModelApiAdapter,
    MetaModelApiConfig,
    MissingApiKeyError,
    UnsupportedAudioError,
)
from willay.adapters.meta_usage import BudgetExceededError, UsageLog

BASE = "https://api.meta.ai/v1"
KEY = "test-key-not-real"

# Real wire lines copied from https://dev.meta.ai/docs/sam/reading-segmentation (frame 0, two objects).
SAM_LINE_0 = (
    "<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/#Xd8%dX9x(|>,"
    "1<|box;x1=155;y1=228;x2=202;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,48,~!!!!J!0c[q=Pj_zs=*4C4(/./x#:/`S_GnD`=o3?X{emCgO$y@|>"
)


def make_adapter(handler, usage_log=None) -> tuple[MetaModelApiAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    config = MetaModelApiConfig(api_key=KEY, base_url=BASE)
    return MetaModelApiAdapter(config, client, usage_log=usage_log), seen


def text_response(text: str, **usage) -> dict:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, **usage},
    }


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(seconds * rate)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


# --- config ------------------------------------------------------------------------------------


def test_config_reads_env_and_defaults_base_url():
    config = MetaModelApiConfig.from_env({"META_API_KEY": "abc"})
    assert config.api_key == "abc"
    assert config.base_url == DEFAULT_BASE_URL == "https://api.meta.ai/v1"


def test_config_reads_custom_base_url_and_strips_trailing_slash():
    config = MetaModelApiConfig.from_env({"META_API_KEY": "abc", "META_API_BASE_URL": "https://example.test/v1/"})
    assert config.base_url == "https://example.test/v1"


@pytest.mark.parametrize("env", [{}, {"META_API_KEY": ""}, {"META_API_KEY": "   "}])
def test_config_requires_api_key(env):
    with pytest.raises(MissingApiKeyError):
        MetaModelApiConfig.from_env(env)


def test_config_repr_does_not_leak_the_key():
    assert KEY not in repr(MetaModelApiConfig(api_key=KEY))


# --- Responses API: text ---------------------------------------------------------------------


def test_rephrase_calls_the_responses_endpoint_with_bearer_auth():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("Rephrased text")))
    assert adapter.rephrase("Original text", "es") == "Rephrased text"

    (request,) = seen
    assert str(request.url) == f"{BASE}/responses"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == MODEL_TEXT == "muse-spark-1.3"
    assert "do not change" in body["instructions"].lower()  # guards facts and tiers
    assert "Original text" in json.dumps(body["input"])
    assert body.get("stream") in (None, False)


def test_explain_uses_the_text_model():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("Because of X.")))
    assert adapter.explain("factor breakdown") == "Because of X."
    assert json.loads(seen[0].content)["model"] == "muse-spark-1.3"


def test_http_error_becomes_meta_api_error_without_leaking_the_key():
    adapter, _ = make_adapter(lambda r: httpx.Response(401, json={"error": {"message": f"bad key {KEY}", "type": "auth"}}))
    with pytest.raises(MetaApiError) as info:
        adapter.rephrase("x")
    assert info.value.status_code == 401
    assert KEY not in str(info.value)


def test_error_message_from_the_api_is_kept_for_debugging():
    adapter, _ = make_adapter(
        lambda r: httpx.Response(400, json={"error": {"message": "only 'auto' is supported", "type": "invalid_request_error"}})
    )
    with pytest.raises(MetaApiError, match="only 'auto' is supported"):
        adapter.rephrase("x")


def test_malformed_response_becomes_meta_api_error():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(MetaApiError):
        adapter.rephrase("x")


def test_transport_errors_are_wrapped_and_redacted():
    def boom(request):
        raise httpx.ConnectError(f"cannot connect with {KEY}")

    adapter, _ = make_adapter(boom)
    with pytest.raises(MetaApiError) as info:
        adapter.rephrase("x")
    assert KEY not in str(info.value)


# --- structured output -----------------------------------------------------------------------


def test_extract_json_sends_text_format_json_schema_and_parses_the_answer():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response('{"n": 18}')))
    result = adapter.extract_json(
        instructions="Extract the number.",
        content=[{"type": "input_text", "text": "N 18"}],
        schema_name="number_only",
        schema=schema,
        purpose="unit",
    )
    assert result == {"n": 18}
    body = json.loads(seen[0].content)
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["name"] == "number_only"
    assert body["text"]["format"]["schema"] == schema
    assert body["input"][0]["role"] == "user"
    assert body["input"][0]["content"] == [{"type": "input_text", "text": "N 18"}]


def test_extract_json_rejects_non_json_output():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json=text_response("not json")))
    with pytest.raises(MetaApiError):
        adapter.extract_json(instructions="x", content=[], schema_name="s", schema={"type": "object"})


def test_extract_json_can_use_another_model():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("{}")))
    adapter.extract_json(instructions="x", content=[], schema_name="s", schema={"type": "object"}, model="muse-wonder-1.0")
    assert json.loads(seen[0].content)["model"] == "muse-wonder-1.0"


# --- tool calling turn ---------------------------------------------------------------------------


def test_run_turn_returns_function_calls_with_parsed_arguments():
    payload = {
        "id": "resp_7",
        "status": "completed",
        "output": [
            {"type": "function_call", "call_id": "call_1", "name": "rank_sites", "arguments": '{"limit": 3}', "status": "completed"}
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=payload))
    tools = [{"type": "function", "name": "rank_sites", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    turn = adapter.run_turn(instructions="be brief", input_items=[{"role": "user", "content": "hola"}], tools=tools)
    assert turn.response_id == "resp_7"
    assert turn.text == ""
    assert [(c.call_id, c.name, c.arguments) for c in turn.tool_calls] == [("call_1", "rank_sites", {"limit": 3})]
    body = json.loads(seen[0].content)
    assert body["tools"] == tools
    assert body["instructions"] == "be brief"
    assert "tool_choice" not in body or body["tool_choice"] == "auto"  # only "auto" is supported


def test_run_turn_chains_with_previous_response_id_and_function_call_output():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("listo")))
    turn = adapter.run_turn(
        instructions="x",
        input_items=[{"type": "function_call_output", "call_id": "call_1", "output": "{}"}],
        tools=[],
        previous_response_id="resp_7",
    )
    assert turn.text == "listo" and turn.tool_calls == ()
    body = json.loads(seen[0].content)
    assert body["previous_response_id"] == "resp_7"
    assert body["input"][0]["type"] == "function_call_output"


def test_run_turn_survives_invalid_tool_arguments_json():
    payload = {
        "id": "r",
        "output": [{"type": "function_call", "call_id": "c", "name": "t", "arguments": "{oops", "status": "completed"}],
    }
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json=payload))
    turn = adapter.run_turn(instructions="x", input_items=[], tools=[])
    assert turn.tool_calls[0].arguments == {}


# --- speech to text ------------------------------------------------------------------------------


def test_transcribe_posts_multipart_request_and_audio_parts_to_asr_transcribe():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json={"sessionId": "s", "transcript": "el río está subiendo", "audioDurationMs": 1000, "turns": []}))
    assert adapter.transcribe(wav_bytes(), "es") == "el río está subiendo"
    (request,) = seen
    assert str(request.url).startswith(f"{BASE}/asr/transcribe")
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert request.headers["accept"] == "application/json"
    raw = request.content
    assert b'name="request"' in raw and b'name="audio"' in raw
    assert MODEL_TRANSCRIBE.encode() in raw and b'"audioEncoding"' in raw and b'"WAV"' in raw
    assert b'"PUSH_TO_TALK"' in raw and b"Spanish" in raw
    assert MODEL_TRANSCRIBE == "muse-voice-transcribe-1.0"


def test_transcribe_rejects_non_wav_audio_before_calling_the_api():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json={"transcript": "x"}))
    with pytest.raises(UnsupportedAudioError):
        adapter.transcribe(b"OggS....not wav", "es")
    assert seen == []


def test_transcribe_rejects_clips_longer_than_ten_minutes():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json={"transcript": "x"}))
    with pytest.raises(UnsupportedAudioError):
        adapter.transcribe(wav_bytes(seconds=601, rate=8000), "es")
    assert seen == []


# --- image generation ----------------------------------------------------------------------------


def test_image_generation_decodes_base64_and_requests_png():
    png = b"\x89PNG-fake"
    adapter, seen = make_adapter(
        lambda r: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}], "usage": {"input_tokens": 9, "output_tokens": 1, "total_tokens": 10}})
    )
    assert adapter.generate("a safe route sign") == png
    (request,) = seen
    assert str(request.url) == f"{BASE}/images/generations"
    body = json.loads(request.content)
    assert body["model"] == MODEL_IMAGE == "muse-image-1.0"
    assert body["n"] == 1 and body["output_format"] == "png"


def test_image_generation_failure_shape():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(MetaApiError):
        adapter.generate("x")


# --- SAM 3.1 video -------------------------------------------------------------------------------


def sam_stream(*lines: str, frames: int | None = 60) -> bytes:
    events = [{"type": "response.created", "response": {"id": "resp_s", "status": "in_progress"}}]
    for line in lines:
        events.append({"type": "response.output_text.delta", "delta": line, "output_index": 0})
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if frames is not None:
        usage["video_frames_processed"] = frames
    events.append({"type": "response.completed", "response": {"id": "resp_s", "status": "completed", "usage": usage}})
    return sse(*events)


def test_segment_video_sends_one_concept_prompt_and_the_clip_as_input_video():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, content=sam_stream(SAM_LINE_0), headers={"content-type": "text/event-stream"}))
    adapter.segment_video(b"fake-mp4-bytes", "water", purpose="unit")
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == f"{BASE}/responses"
    assert body["model"] == MODEL_SEGMENTATION == "sam-3.1"
    assert body["stream"] is True
    (message,) = body["input"]
    assert message["type"] == "message" and message["role"] == "user"
    text_part, video_part = message["content"]
    assert text_part == {"type": "input_text", "text": "water"}
    assert video_part["type"] == "input_video"
    assert video_part["video_url"] == "data:video/mp4;base64," + base64.b64encode(b"fake-mp4-bytes").decode()
    assert body["metadata"]["mask_encoding"] == "one_bit"
    assert body["metadata"]["include_confidence"] == "true"


def test_segment_video_parses_frames_objects_boxes_and_masks():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=sam_stream(SAM_LINE_0, frames=60)))
    seg = adapter.segment_video(b"x", "button")
    assert (seg.width, seg.height) == (320, 334)
    assert seg.billed_frames == 60
    (frame,) = seg.frames
    assert frame.frame_index == 0
    first, second = frame.objects
    assert (first.object_id, second.object_id) == ("0", "1")
    assert first.box == (211, 228, 271, 255)  # half-open
    assert first.mask.count() == 1539 and second.mask.count() == 1236
    assert first.mask.bbox() is not None
    x1, y1, x2, y2 = first.mask.bbox()
    assert x1 >= 211 and y1 >= 228 and x2 <= 271 and y2 <= 255
    assert (first.mask.width, first.mask.height) == (320, 334)


def test_segment_video_zero_matches_is_a_normal_result():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=sam_stream(frames=50)))
    seg = adapter.segment_video(b"x", "unicorn")
    assert seg.frames == () and seg.billed_frames == 50


def test_segment_video_stream_error_event_raises():
    err = sse({"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error", "message": "boom"}}})
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=err))
    with pytest.raises(MetaApiError, match="boom"):
        adapter.segment_video(b"x", "water")


def test_segment_video_http_400_raises_with_api_message():
    adapter, _ = make_adapter(lambda r: httpx.Response(400, json={"error": {"message": "invalid prompt: x", "type": "invalid_request_error"}}))
    with pytest.raises(MetaApiError, match="invalid prompt") as info:
        adapter.segment_video(b"x", "water")
    assert info.value.status_code == 400


# --- usage log and budget guard ---------------------------------------------------------------------


def test_each_call_is_logged_with_model_tokens_and_estimated_cost(tmp_path):
    log = UsageLog(tmp_path / "meta_usage.jsonl")
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json=text_response("ok", input_tokens=1000, output_tokens=200)), usage_log=log)
    adapter.rephrase("x")
    entry = json.loads((tmp_path / "meta_usage.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["model"] == "muse-spark-1.3" and entry["endpoint"] == "/responses"
    assert entry["input_tokens"] == 1000 and entry["output_tokens"] == 200
    assert entry["est_cost_usd"] == pytest.approx((1000 * 1.25 + 200 * 4.25) / 1e6)
    assert KEY not in json.dumps(entry)


def test_sam_call_is_logged_with_billed_frames(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=sam_stream(SAM_LINE_0, frames=80)), usage_log=log)
    adapter.segment_video(b"x", "water", purpose="watch")
    entry = json.loads((tmp_path / "u.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["model"] == "sam-3.1" and entry["frames"] == 80
    assert entry["est_cost_usd"] == pytest.approx(80 * 0.0002)
    assert entry["purpose"] == "watch"


def test_image_and_asr_calls_are_logged(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    png = base64.b64encode(b"png").decode()
    adapter, _ = make_adapter(
        lambda r: httpx.Response(200, json={"data": [{"b64_json": png}]} if r.url.path.endswith("generations") else {"transcript": "hola", "audioDurationMs": 2500}),
        usage_log=log,
    )
    adapter.generate("x")
    adapter.transcribe(wav_bytes(2.5), "es")
    entries = [json.loads(l) for l in (tmp_path / "u.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entries[0]["model"] == "muse-image-1.0" and entries[0]["est_cost_usd"] == pytest.approx(0.01)
    assert entries[1]["model"] == "muse-voice-transcribe-1.0" and entries[1]["audio_seconds"] == pytest.approx(2.5)


def test_over_budget_call_is_refused_before_any_request_is_sent(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", budget_usd=0.001)
    log.record(model="m", endpoint="/e", purpose="p", est_cost_usd=0.001)
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("ok")), usage_log=log)
    with pytest.raises(BudgetExceededError):
        adapter.generate("x")
    assert seen == []


def test_adapter_never_hits_the_network_when_given_a_mock_transport():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("ok")))
    adapter.rephrase("x")
    assert all(r.url.host == "api.meta.ai" for r in seen)


def test_run_turn_can_pick_the_model_cap_output_and_reports_usage():
    payload = dict(text_response("ok"), usage={"input_tokens": 120, "output_tokens": 30, "input_tokens_details": {"cached_tokens": 20}})
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=payload))
    turn = adapter.run_turn(instructions="x", input_items=[], tools=[], model="muse-wonder-1.0", max_output_tokens=900)
    body = json.loads(seen[0].content)
    assert body["model"] == "muse-wonder-1.0" and body["max_output_tokens"] == 900
    assert (turn.model, turn.input_tokens, turn.output_tokens, turn.cached_tokens) == ("muse-wonder-1.0", 120, 30, 20)


def test_run_turn_defaults_to_the_text_model():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json=text_response("ok")))
    turn = adapter.run_turn(instructions="x", input_items=[], tools=[])
    assert json.loads(seen[0].content)["model"] == MODEL_TEXT and turn.model == MODEL_TEXT


# --- transient failures ---------------------------------------------------------------------------------------------


def adapter_with_script(statuses, sleeps, usage_log=None):
    """Answers with the scripted HTTP statuses in order; the last one repeats."""
    calls = []

    def handler(request):
        calls.append(request)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        if status == 200:
            return httpx.Response(200, json=text_response("ok"))
        if status == "stale":
            return httpx.Response(400, json={"error": {"message": "referenced response not found or expired"}})
        return httpx.Response(status, json={"error": {"message": "persistence service error"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = MetaModelApiAdapter(MetaModelApiConfig(api_key=KEY, base_url=BASE), client, usage_log=usage_log, sleep=sleeps.append)
    return adapter, calls


def test_responses_retry_transient_server_errors_with_backoff():
    sleeps = []
    adapter, calls = adapter_with_script([503, 429, 200], sleeps)
    assert adapter.run_turn(instructions="x", input_items=[], tools=[]).text == "ok"
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0


def test_a_just_created_response_that_is_not_found_yet_is_retried():
    sleeps = []
    adapter, calls = adapter_with_script(["stale", 200], sleeps)
    turn = adapter.run_turn(instructions="x", input_items=[], tools=[], previous_response_id="resp_9")
    assert turn.text == "ok" and len(calls) == 2


def test_other_client_errors_are_not_retried():
    sleeps = []
    adapter, calls = adapter_with_script([401], sleeps)
    with pytest.raises(MetaApiError):
        adapter.run_turn(instructions="x", input_items=[], tools=[])
    assert len(calls) == 1 and sleeps == []


def test_retries_are_bounded_and_the_last_error_is_raised():
    sleeps = []
    adapter, calls = adapter_with_script([503], sleeps)
    with pytest.raises(MetaApiError) as error:
        adapter.run_turn(instructions="x", input_items=[], tools=[])
    assert len(calls) == 3 and error.value.status_code == 503


def test_a_retried_call_is_logged_and_billed_once(tmp_path):
    log = UsageLog(tmp_path / "usage.jsonl", budget_usd=1.0)
    adapter, _ = adapter_with_script([503, 200], [], usage_log=log)
    adapter.run_turn(instructions="x", input_items=[], tools=[])
    assert len((tmp_path / "usage.jsonl").read_text().strip().splitlines()) == 1


# --- SAM 3.1 still images -------------------------------------------------------------------------------


def test_segment_image_sends_one_concept_prompt_and_the_picture_as_input_image():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, content=sam_stream(SAM_LINE_0, frames=None)))
    adapter.segment_image(b"fake-jpeg", "staff gauge", purpose="unit")
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == f"{BASE}/responses"
    assert body["model"] == "sam-3.1" and body["stream"] is True
    text_part, image_part = body["input"][0]["content"]
    assert text_part == {"type": "input_text", "text": "staff gauge"}
    assert image_part["type"] == "input_image"
    assert image_part["image_url"] == "data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode()
    assert body["metadata"]["mask_encoding"] == "one_bit"


def test_segment_image_parses_objects_into_a_single_frame():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=sam_stream(SAM_LINE_0, frames=None)))
    seg = adapter.segment_image(b"x", "button")
    assert (seg.width, seg.height) == (320, 334)
    (frame,) = seg.frames
    assert frame.frame_index == 0 and len(frame.objects) == 2
    assert frame.objects[0].mask.count() == 1539


def test_segment_image_zero_matches_is_a_normal_result():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=sam_stream(frames=None)))
    assert adapter.segment_image(b"x", "unicorn").frames == ()


def test_segment_image_is_logged_as_one_image_at_the_image_price(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    adapter, _ = make_adapter(lambda r: httpx.Response(200, content=sam_stream(SAM_LINE_0, frames=None)), usage_log=log)
    adapter.segment_image(b"x", "water", purpose="gauge_validation")
    entry = json.loads((tmp_path / "u.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["model"] == "sam-3.1" and entry["images"] == 1 and "frames" not in entry
    assert entry["est_cost_usd"] == pytest.approx(0.0025)
    assert entry["purpose"] == "gauge_validation"


def test_segment_image_respects_the_budget_guard(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", budget_usd=0.001)
    adapter, seen = make_adapter(lambda r: httpx.Response(200, content=sam_stream(frames=None)), usage_log=log)
    with pytest.raises(BudgetExceededError):
        adapter.segment_image(b"x", "water")
    assert seen == []

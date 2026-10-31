"""Unit tests for the Meta model API adapter. A mocked HTTP transport is used: no network."""

import base64
import json

import httpx
import pytest

from willay.adapters.meta_model_api import (
    DEFAULT_BASE_URL,
    MODEL_IMAGE,
    MODEL_SEGMENTATION,
    MODEL_TEXT,
    MODEL_TRANSCRIBE,
    MetaApiError,
    MetaModelApiConfig,
    MetaModelApiAdapter,
    MissingApiKeyError,
)

BASE = "https://api.meta.ai/v1"


def make_adapter(handler) -> tuple[MetaModelApiAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    config = MetaModelApiConfig(api_key="test-key-not-real", base_url=BASE)
    return MetaModelApiAdapter(config, client), seen


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
    assert "test-key-not-real" not in repr(MetaModelApiConfig(api_key="test-key-not-real"))


def test_rephrase_calls_chat_completions_with_text_model_and_bearer_auth():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Rephrased text"}}]})

    adapter, seen = make_adapter(handler)
    assert adapter.rephrase("Original text", "es") == "Rephrased text"

    (request,) = seen
    assert str(request.url) == f"{BASE}/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key-not-real"
    body = json.loads(request.content)
    assert body["model"] == MODEL_TEXT == "muse-spark-1.3"
    user_message = body["messages"][-1]["content"]
    assert "Original text" in user_message
    system_message = body["messages"][0]["content"].lower()
    assert "do not change" in system_message  # guards facts and tiers


def test_explain_uses_the_text_model():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Because of X."}}]})

    adapter, seen = make_adapter(handler)
    assert adapter.explain("factor breakdown") == "Because of X."
    assert json.loads(seen[0].content)["model"] == "muse-spark-1.3"


def test_http_error_becomes_meta_api_error():
    adapter, _ = make_adapter(lambda r: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(MetaApiError) as info:
        adapter.rephrase("x")
    assert info.value.status_code == 401
    assert "test-key-not-real" not in str(info.value)


def test_malformed_response_becomes_meta_api_error():
    adapter, _ = make_adapter(lambda r: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(MetaApiError):
        adapter.rephrase("x")


def test_transcribe_posts_multipart_audio_with_transcribe_model():
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json={"text": "el rio esta subiendo"}))
    assert adapter.transcribe(b"fake-audio-bytes", "es") == "el rio esta subiendo"
    (request,) = seen
    assert str(request.url) == f"{BASE}/audio/transcriptions"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert MODEL_TRANSCRIBE.encode() in request.content
    assert MODEL_TRANSCRIBE == "muse-voice-transcribe-1.0"


def test_image_generation_decodes_base64_payload():
    png = b"\x89PNG-fake"
    adapter, seen = make_adapter(
        lambda r: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode()}]})
    )
    assert adapter.generate("a safe route sign") == png
    (request,) = seen
    assert str(request.url) == f"{BASE}/images/generations"
    assert json.loads(request.content)["model"] == MODEL_IMAGE == "muse-image-1.0"


def test_segmentation_is_a_declared_skeleton():
    adapter, seen = make_adapter(lambda r: httpx.Response(500))
    with pytest.raises(NotImplementedError):
        adapter.measure_water_rise_cm(b"a", b"b")
    assert seen == []
    assert MODEL_SEGMENTATION == "sam-3.1"


def test_adapter_never_hits_the_network_when_given_a_mock_transport():
    # The mock transport is the only transport; an unexpected host would still be served by the mock.
    adapter, seen = make_adapter(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    adapter.rephrase("x")
    assert all(r.url.host == "api.meta.ai" for r in seen)

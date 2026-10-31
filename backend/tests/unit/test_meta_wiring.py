"""Live Meta capabilities are opt-in: nothing is wired (and no .env is read) unless WILLAY_META_ENABLED=1."""

import willay.api.dependencies as dependencies
from willay.api.dependencies import attach_meta_from_env, synthetic_dependencies


def test_meta_is_not_wired_by_default():
    deps = attach_meta_from_env(synthetic_dependencies(), environ={})
    assert deps.agent_model is None and deps.transcriber is None and deps.video_segmenter is None
    assert deps.image_generator is None and deps.extractor is None


def test_enabling_wires_one_adapter_into_every_capability(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("willay.adapters.meta_model_api.load_adapter_from_env", lambda *a, **k: sentinel)
    deps = attach_meta_from_env(synthetic_dependencies(), environ={"WILLAY_META_ENABLED": "1"})
    for field in ("agent_model", "transcriber", "video_segmenter", "image_generator", "extractor"):
        assert getattr(deps, field) is sentinel


def test_a_missing_key_leaves_the_app_running_without_meta(monkeypatch):
    from willay.adapters.meta_model_api import MissingApiKeyError

    def boom(*a, **k):
        raise MissingApiKeyError("META_API_KEY is not set")

    monkeypatch.setattr("willay.adapters.meta_model_api.load_adapter_from_env", boom)
    deps = attach_meta_from_env(synthetic_dependencies(), environ={"WILLAY_META_ENABLED": "1"})
    assert deps.agent_model is None


def test_default_dependencies_do_not_enable_meta_in_tests():
    assert dependencies.default_dependencies().agent_model is None

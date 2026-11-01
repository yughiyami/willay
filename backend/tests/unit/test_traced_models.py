"""Wrappers that record every model call (planner, specialists, SAM, image, voice) in the active run trace."""

import pytest

from willay.adapters.meta_usage import sam_cost
from willay.adapters.traced_models import TracedAgentModel, TracedImage, TracedSegmenter, TracedTranscriber
from willay.application.orchestration.trace import TraceStore, use_agent, use_trace
from willay.domain.segmentation import VideoSegmentation
from willay.ports.agent import AgentTurn


class FakeModel:
    def __init__(self, turn=None, error=None):
        self.turn, self.error, self.kwargs = turn, error, None

    def run_turn(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.turn


def test_agent_model_calls_are_traced_with_agent_model_tokens_and_cost():
    turn = AgentTurn("r1", "hola", model="muse-wonder-1.0", input_tokens=1000, output_tokens=500)
    traced = TracedAgentModel(FakeModel(turn), default_model="muse-spark-1.3")
    trace = TraceStore().create()
    with use_trace(trace), use_agent("planner"):
        result = traced.run_turn(instructions="i", input_items=[], tools=[], model="muse-wonder-1.0")
    assert result is turn
    (event,) = trace.snapshot()["events"]
    assert event["kind"] == "model_call" and event["agent"] == "planner" and event["model"] == "muse-wonder-1.0"
    assert event["input_tokens"] == 1000 and event["output_tokens"] == 500
    assert event["cost_usd"] == pytest.approx((1000 * 4.0 + 500 * 20.0) / 1_000_000)
    assert event["latency_ms"] >= 0


def test_the_requested_model_is_forwarded_and_defaults_apply():
    inner = FakeModel(AgentTurn("r", "x"))
    traced = TracedAgentModel(inner, default_model="muse-spark-1.3")
    traced.run_turn(instructions="i", input_items=[], tools=[])
    assert inner.kwargs.get("model") in (None, "muse-spark-1.3")


def test_without_an_active_trace_calls_pass_through_silently():
    traced = TracedAgentModel(FakeModel(AgentTurn("r", "x")), default_model="m")
    assert traced.run_turn(instructions="i", input_items=[], tools=[]).text == "x"


def test_errors_are_recorded_then_re_raised():
    traced = TracedAgentModel(FakeModel(error=RuntimeError("boom")), default_model="muse-wonder-1.0")
    trace = TraceStore().create()
    with use_trace(trace), use_agent("planner"), pytest.raises(RuntimeError):
        traced.run_turn(instructions="i", input_items=[], tools=[], model="muse-wonder-1.0")
    (event,) = trace.snapshot()["events"]
    assert event["status"] == "error" and event["model"] == "muse-wonder-1.0"


class FakeSegmenter:
    def segment_video(self, video, prompt, purpose=""):
        return VideoSegmentation(prompt=prompt, width=4, height=4, frames=(), billed_frames=60)


def test_segmentation_is_traced_as_sam_with_frame_based_cost():
    trace = TraceStore().create()
    with use_trace(trace), use_agent("watcher"):
        TracedSegmenter(FakeSegmenter()).segment_video(b"x", "water", purpose="watch_water")
    (event,) = trace.snapshot()["events"]
    assert event["model"] == "sam-3.1" and event["agent"] == "watcher"
    assert event["cost_usd"] == pytest.approx(sam_cost(frames=60))
    assert "prompt=water" in event["args_summary"]


def test_image_and_transcription_are_traced():
    class Img:
        def generate(self, prompt, size=None):
            return b"png"

    class Tr:
        def transcribe(self, audio, language="es"):
            return "hola"

    trace = TraceStore().create()
    with use_trace(trace), use_agent("communicator"):
        assert TracedImage(Img()).generate("prompt with text", "1024x1536") == b"png"
        assert TracedTranscriber(Tr()).transcribe(b"x" * 100, "es") == "hola"
    models = [e["model"] for e in trace.snapshot()["events"]]
    assert models == ["muse-image-1.0", "muse-voice-transcribe-1.0"]
    assert trace.snapshot()["events"][0]["cost_usd"] == pytest.approx(0.01)

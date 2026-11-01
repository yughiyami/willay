"""Recorded model turns: capture a real run once, replay it offline with no network and no cost."""

import pytest

from willay.adapters.replay_models import RecordingAgentModel, ReplayAgentModel, ReplayImage, ReplayMismatch
from willay.adapters.traced_models import TracedAgentModel
from willay.application.orchestration.trace import TraceStore, use_agent, use_trace
from willay.ports.agent import AgentTurn, ToolCall

RECORDING = {
    "calls": [
        {"agent": "planner", "model": "muse-wonder-1.0", "text": "", "input_tokens": 900, "output_tokens": 120,
         "tool_calls": [{"call_id": "c1", "name": "ask_dispatcher", "arguments": {"task": "x", "site_id": "$SITE_ID"}}]},
        {"agent": "planner", "model": "muse-wonder-1.0", "text": "Listo.", "input_tokens": 1200, "output_tokens": 60, "tool_calls": []},
    ]
}


def test_replay_returns_the_recorded_turns_in_order_with_the_site_substituted():
    replay = ReplayAgentModel(RECORDING, site_id="ingemmet-1015", pace_s=0.0)
    first = replay.run_turn(instructions="i", input_items=[], tools=[], purpose="planner", model="muse-wonder-1.0")
    assert first.tool_calls[0].name == "ask_dispatcher" and first.tool_calls[0].arguments["site_id"] == "ingemmet-1015"
    assert first.model == "muse-wonder-1.0" and first.input_tokens == 900
    second = replay.run_turn(instructions="i", input_items=[], tools=[], purpose="planner")
    assert second.text == "Listo."


def test_replay_swaps_the_site_placeholder_in_texts_too():
    recording = {"calls": [{"agent": "planner", "model": "m", "text": "El relave $SITE_ID esta en PREPARATION.", "tool_calls": []}]}
    turn = ReplayAgentModel(recording, site_id="ingemmet-1015", pace_s=0.0).run_turn(instructions="i", input_items=[], tools=[], purpose="planner")
    assert turn.text == "El relave ingemmet-1015 esta en PREPARATION."


def test_replay_refuses_a_run_that_diverged_from_the_recording():
    replay = ReplayAgentModel(RECORDING, site_id="s", pace_s=0.0)
    with pytest.raises(ReplayMismatch):
        replay.run_turn(instructions="i", input_items=[], tools=[], purpose="watcher")


def test_replay_runs_out_cleanly():
    replay = ReplayAgentModel({"calls": []}, site_id="s", pace_s=0.0)
    with pytest.raises(ReplayMismatch):
        replay.run_turn(instructions="i", input_items=[], tools=[], purpose="planner")


def test_replayed_calls_are_traced_as_free_and_marked_replayed():
    traced = TracedAgentModel(ReplayAgentModel(RECORDING, site_id="s", pace_s=0.0), replayed=True)
    trace = TraceStore().create()
    with use_trace(trace), use_agent("planner"):
        traced.run_turn(instructions="i", input_items=[], tools=[], purpose="planner")
    (event,) = trace.snapshot()["events"]
    assert event["cost_usd"] == 0.0 and event["detail"]["replayed"] is True and event["model"] == "muse-wonder-1.0"


class Inner:
    def __init__(self):
        self.turns = [
            AgentTurn("r1", "", (ToolCall("c1", "prepare_dispatch", {"site_id": "ingemmet-1015"}),), model="muse-spark-1.3", input_tokens=10, output_tokens=5),
            AgentTurn("r2", "ok", model="muse-spark-1.3", input_tokens=20, output_tokens=7),
        ]

    def run_turn(self, **kwargs):
        return self.turns.pop(0)


def test_recording_captures_turns_and_swaps_the_site_id_for_a_placeholder():
    recorder = RecordingAgentModel(Inner(), site_id="ingemmet-1015")
    recorder.run_turn(instructions="i", input_items=[], tools=[], purpose="dispatcher", model="muse-spark-1.3")
    recorder.run_turn(instructions="i", input_items=[], tools=[], purpose="dispatcher", model="muse-spark-1.3")
    data = recorder.to_dict()
    assert [c["agent"] for c in data["calls"]] == ["dispatcher", "dispatcher"]
    assert data["calls"][0]["tool_calls"][0]["arguments"] == {"site_id": "$SITE_ID"}
    assert data["calls"][1]["text"] == "ok" and data["calls"][1]["output_tokens"] == 7


def test_a_recording_round_trips_through_replay():
    recorder = RecordingAgentModel(Inner(), site_id="ingemmet-1015")
    for _ in range(2):
        recorder.run_turn(instructions="i", input_items=[], tools=[], purpose="dispatcher")
    replay = ReplayAgentModel(recorder.to_dict(), site_id="ingemmet-1015", pace_s=0.0)
    assert replay.run_turn(instructions="i", input_items=[], tools=[], purpose="dispatcher").tool_calls[0].arguments["site_id"] == "ingemmet-1015"


def test_replay_image_returns_the_recorded_card():
    assert ReplayImage(b"\x89PNG-card").generate("any prompt", "1024x1536") == b"\x89PNG-card"


# --- recorded SAM output ---------------------------------------------------------------------------------------


def test_recorded_segmentation_round_trips_masks_boxes_and_billing():
    from willay.adapters.replay_models import RecordingSegmenter, ReplaySegmenter
    from willay.domain.segmentation import FrameSegmentation, Mask, TrackedObject, VideoSegmentation

    mask = Mask.rectangle(40, 30, 5, 6, 20, 25)
    original = VideoSegmentation(
        prompt="water", width=40, height=30, billed_frames=60,
        frames=(FrameSegmentation(3, (TrackedObject("1", (5, 6, 20, 25), mask, 0.87),)),),
    )

    class Segmenter:
        def segment_video(self, video, prompt, purpose=""):
            return original

    recorder = RecordingSegmenter(Segmenter())
    recorder.segment_video(b"clip", "water", purpose="watch_water")
    data = recorder.to_dict()
    assert len(str(data)) < 2000  # a flat shape compresses to a few hundred bytes
    replayed = ReplaySegmenter(data).segment_video(b"other bytes", "water")
    assert replayed == original


def test_replay_segmenter_refuses_a_prompt_that_was_not_recorded():
    from willay.adapters.replay_models import ReplaySegmenter

    with pytest.raises(ReplayMismatch):
        ReplaySegmenter({}).segment_video(b"x", "water")

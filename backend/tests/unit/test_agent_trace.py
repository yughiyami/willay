"""Run traces: every model call and tool call is recorded without PII and can be streamed."""

import threading

from willay.application.orchestration.trace import (
    TraceStore,
    current_agent,
    current_trace,
    summarize_args,
    use_agent,
    use_trace,
)


def test_summarize_args_keeps_ids_and_numbers_but_not_free_text_or_contacts():
    summary = summarize_args(
        {
            "site_id": "ingemmet-1015",
            "limit": 5,
            "visible": True,
            "task": "Llama a Juan Perez al +51 999 888 777 o escribe a juan@example.com para avisar",
            "phone": "+51999888777",
        }
    )
    assert "site_id=ingemmet-1015" in summary
    assert "limit=5" in summary and "visible=True" in summary
    assert "Juan" not in summary and "example.com" not in summary and "999" not in summary
    assert "task=<text" in summary
    assert "phone=<redacted>" in summary


def test_trace_events_are_sequential_and_totals_add_up():
    store = TraceStore()
    trace = store.create()
    trace.emit("model_call", agent="planner", model="muse-wonder-1.0", latency_ms=120, cost_usd=0.01, input_tokens=100, output_tokens=40)
    trace.emit("tool_call", agent="planner", tool="ask_hazard_analyst", args_summary="task=<text 10 chars>", latency_ms=900)
    trace.emit("model_call", agent="hazard_analyst", model="muse-spark-1.3", latency_ms=80, cost_usd=0.002)
    trace.finish("completed", {"answer_es": "ok"})
    snapshot = trace.snapshot()
    assert [e["seq"] for e in snapshot["events"]] == [1, 2, 3, 4]
    assert snapshot["events"][-1]["kind"] == "run_end"
    assert snapshot["status"] == "completed"
    assert snapshot["totals"]["cost_usd"] == 0.012
    assert snapshot["totals"]["model_calls"] == 2
    assert snapshot["totals"]["tool_calls"] == 1
    assert set(snapshot["totals"]["models"]) == {"muse-wonder-1.0", "muse-spark-1.3"}
    assert snapshot["result"] == {"answer_es": "ok"}


def test_store_returns_runs_by_id_and_evicts_the_oldest():
    store = TraceStore(max_runs=2)
    first, second, third = store.create(), store.create(), store.create()
    assert store.get(first.run_id) is None
    assert store.get(second.run_id) is second and store.get(third.run_id) is third
    assert store.get("missing") is None


def test_streaming_replays_past_events_then_follows_live_ones_until_the_run_ends():
    trace = TraceStore().create()
    trace.emit("run_start")
    seen = []

    def consume():
        for event in trace.iter_events(poll_s=0.01):
            seen.append(event.kind)

    reader = threading.Thread(target=consume)
    reader.start()
    trace.emit("tool_call", tool="rank_sites")
    trace.finish("completed", {})
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert seen[0] == "run_start" and "tool_call" in seen and seen[-1] == "run_end"


def test_context_helpers_scope_the_active_trace_and_agent():
    trace = TraceStore().create()
    assert current_trace() is None and current_agent() == ""
    with use_trace(trace):
        with use_agent("watcher"):
            assert current_trace() is trace and current_agent() == "watcher"
        assert current_agent() == ""
    assert current_trace() is None

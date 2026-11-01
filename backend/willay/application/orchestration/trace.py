"""Run traces for the multi-agent orchestrator.

Every model call, tool call, delegation, guardrail and operator action of a run is appended to a `RunTrace`:
agent, model, tool, a PII-free summary of the arguments, latency and cost. The web console follows a run live
(Server-Sent Events over `iter_events`) and `GET /agent/trace/{run_id}` serves the full record afterwards.

The active trace and the active agent travel in context variables, so model adapters (planner, specialists, SAM,
image, voice) can record themselves without knowing about runs.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Mapping

_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,48}$")
_SENSITIVE_KEYS = ("phone", "email", "mail", "address", "contact", "household_label", "whatsapp", "dni")
_SHORT_TEXT = 40

_active_trace: ContextVar["RunTrace | None"] = ContextVar("willay_active_trace", default=None)
_active_agent: ContextVar[str] = ContextVar("willay_active_agent", default="")


def current_trace() -> "RunTrace | None":
    return _active_trace.get()


def current_agent() -> str:
    return _active_agent.get()


@contextmanager
def use_trace(trace: "RunTrace | None") -> Iterator[None]:
    token = _active_trace.set(trace)
    try:
        yield
    finally:
        _active_trace.reset(token)


@contextmanager
def use_agent(name: str) -> Iterator[None]:
    token = _active_agent.set(name)
    try:
        yield
    finally:
        _active_agent.reset(token)


def summarize_args(args: Mapping[str, object], safe_text_keys: tuple[str, ...] = (), max_len: int = 240) -> str:
    """One line describing tool arguments without personal data.

    Ids, numbers and booleans are shown. Free text is replaced by its length (an operator's sentence may name a
    person), sensitive keys are redacted, and only keys listed in `safe_text_keys` may show a short phrase.
    """
    parts: list[str] = []
    for key, value in args.items():
        lowered = str(key).lower()
        if any(s in lowered for s in _SENSITIVE_KEYS):
            parts.append(f"{key}=<redacted>")
        elif isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif isinstance(value, str):
            if _ID.match(value):
                parts.append(f"{key}={value}")
            elif key in safe_text_keys and len(value) <= _SHORT_TEXT and "@" not in value and not re.search(r"\d{5,}", value):
                parts.append(f"{key}={value}")
            else:
                parts.append(f"{key}=<text {len(value)} chars>")
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key}=[{len(value)} items]")
        elif isinstance(value, Mapping):
            parts.append(f"{key}={{{','.join(sorted(map(str, value))[:6])}}}")
        else:
            parts.append(f"{key}=<{type(value).__name__}>")
    text = " ".join(parts)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


@dataclass(frozen=True)
class TraceEvent:
    seq: int
    ts: str
    run_id: str
    kind: str  # run_start | agent_start | agent_end | model_call | tool_call | guardrail | step | proposal | operator_action | run_end
    agent: str = ""
    model: str = ""
    tool: str = ""
    args_summary: str = ""
    result_summary: str = ""
    status: str = "ok"  # ok | error
    latency_ms: int | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class RunTrace:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.status = "running"
        self.result: Mapping[str, object] | None = None
        self.artifacts: dict[str, tuple[str, bytes]] = {}
        self._events: list[TraceEvent] = []
        self._cond = threading.Condition()

    def emit(self, kind: str, **fields: object) -> TraceEvent:
        fields.setdefault("agent", current_agent())
        with self._cond:
            event = TraceEvent(
                seq=len(self._events) + 1,
                ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                run_id=self.run_id,
                kind=kind,
                **fields,  # type: ignore[arg-type]
            )
            self._events.append(event)
            self._cond.notify_all()
        return event

    def finish(self, status: str, result: Mapping[str, object] | None = None) -> None:
        self.emit("run_end", status="ok" if status == "completed" else "error", result_summary=status)
        with self._cond:
            self.status = status
            self.result = result
            self._cond.notify_all()

    def add_artifact(self, name: str, content_type: str, data: bytes) -> None:
        with self._cond:
            self.artifacts[name] = (content_type, data)

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def events(self) -> list[TraceEvent]:
        with self._cond:
            return list(self._events)

    def totals(self) -> dict:
        events = self.events()
        models = sorted({e.model for e in events if e.kind == "model_call" and e.model})
        return {
            "cost_usd": round(sum(e.cost_usd or 0.0 for e in events), 6),
            "model_calls": sum(1 for e in events if e.kind == "model_call"),
            "tool_calls": sum(1 for e in events if e.kind == "tool_call"),
            "models": models,
            "agents": sorted({e.agent for e in events if e.agent}),
        }

    def total_cost(self) -> float:
        return sum(e.cost_usd or 0.0 for e in self.events())

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "events": [e.to_dict() for e in self.events()],
            "totals": self.totals(),
            "result": None if self.result is None else dict(self.result),
            "artifacts": sorted(self.artifacts),
        }

    def iter_events(self, after: int = 0, poll_s: float = 0.25) -> Iterator[TraceEvent]:
        """Yield events from sequence `after` + 1 on, waiting for new ones until the run has finished."""
        index = after
        while True:
            with self._cond:
                while index >= len(self._events) and self.status == "running":
                    self._cond.wait(timeout=poll_s)
                batch = self._events[index:]
                done = self.status != "running"
            for event in batch:
                yield event
            index += len(batch)
            if done and not batch:
                return


class TraceStore:
    def __init__(self, max_runs: int = 100) -> None:
        self._max = max_runs
        self._runs: "OrderedDict[str, RunTrace]" = OrderedDict()
        self._lock = threading.Lock()

    def create(self, run_id: str | None = None) -> RunTrace:
        trace = RunTrace(run_id or uuid.uuid4().hex[:12])
        with self._lock:
            self._runs[trace.run_id] = trace
            while len(self._runs) > self._max:
                self._runs.popitem(last=False)
        return trace

    def get(self, run_id: str) -> RunTrace | None:
        with self._lock:
            return self._runs.get(run_id)

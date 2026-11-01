from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class AgentTurn:
    response_id: str
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    # Metadata for tracing and cost control; adapters that do not report usage leave the defaults.
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class AgentModelPort(Protocol):
    """One turn of a tool-calling language model. The model may call tools; it never decides alert tiers."""

    def run_turn(
        self,
        *,
        instructions: str,
        input_items: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]],
        previous_response_id: str | None = None,
        purpose: str = "",
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> AgentTurn: ...


class StructuredExtractionPort(Protocol):
    """Schema-constrained JSON extraction from documents (e.g. an official PDF)."""

    def extract_json(
        self,
        *,
        instructions: str,
        content: Sequence[Mapping[str, object]],
        schema_name: str,
        schema: Mapping[str, object],
        purpose: str = "",
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> dict: ...

"""Simulated telephony: no call is ever placed. Used whenever WILLAY_CALLS_ENABLED is not 1."""

from __future__ import annotations

from typing import Mapping

from willay.domain.dispatch import ResponseKind
from willay.domain.entities import AlertTier
from willay.ports.calls import CallResult


class SimulatedCallAdapter:
    """Answers with a scripted outcome per number (default NO_ANSWER) and records every attempt."""

    def __init__(
        self, outcomes: Mapping[str, ResponseKind] | None = None, default: ResponseKind = ResponseKind.NO_ANSWER
    ) -> None:
        self._outcomes = dict(outcomes or {})
        self._default = default
        self.calls: list[tuple[str, AlertTier]] = []

    def call(self, number: str, tier: AlertTier) -> CallResult:
        self.calls.append((number, tier))
        return CallResult(self._outcomes.get(number, self._default), "simulated: no real call was placed")

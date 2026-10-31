from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from willay.domain.dispatch import ResponseKind
from willay.domain.entities import AlertTier


class CallNotAllowed(Exception):
    """The number is not on the allow-list (or is not a valid E.164 number): no call is placed."""


class CallError(Exception):
    """The telephony platform failed (unreachable, rejected the request, misconfigured)."""


@dataclass(frozen=True)
class CallResult:
    outcome: ResponseKind
    detail: str = ""


class CallPort(Protocol):
    """Place one automated voice call that plays the prerecorded message of a tier and collects 1 or 2.

    Blocks until the call ends. Raises CallNotAllowed for a number outside the allow-list and CallError when
    the platform fails. An unanswered call or a missing key press is NOT an error: it returns NO_ANSWER.
    """

    def call(self, number: str, tier: AlertTier) -> CallResult: ...

"""Domain vocabulary of the alert loop: household answers and the phone keys that produce them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResponseKind(Enum):
    """What a household answered (or did not) after an alert."""

    SAFE = "SAFE"
    NEEDS_HELP = "NEEDS_HELP"
    NO_ANSWER = "NO_ANSWER"

    @property
    def needs_operator(self) -> bool:
        """NEEDS_HELP and NO_ANSWER are escalated to a human operator."""
        return self is not ResponseKind.SAFE


# On the phone: 1 = I am safe, 2 = I need help. Any other key is ignored.
DTMF_RESPONSES = {"1": ResponseKind.SAFE, "2": ResponseKind.NEEDS_HELP}


def response_from_digit(digit: str) -> ResponseKind | None:
    return DTMF_RESPONSES.get(digit)


@dataclass(frozen=True)
class Household:
    """A SYNTHETIC demo household (never a real person). `phone` is E.164."""

    id: str
    settlement_id: str
    label: str
    phone: str
    language: str = "es"
    # Only for the simulated call adapter: how this synthetic household "answers" a call.
    demo_call_outcome: ResponseKind = ResponseKind.NO_ANSWER

    def __post_init__(self) -> None:
        if not self.id or not self.settlement_id:
            raise ValueError("a household needs an id and a settlement_id")

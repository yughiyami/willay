from __future__ import annotations

from typing import Protocol


class LLMPort(Protocol):
    """Language model used ONLY to rephrase or explain. It never decides alert tiers."""

    def rephrase(self, text: str, language: str = "en") -> str: ...

    def explain(self, facts: str, language: str = "en") -> str: ...

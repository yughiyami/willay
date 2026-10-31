from __future__ import annotations

from typing import Protocol


class ImagePort(Protocol):
    """Image generation for illustrative material (never used for decisions)."""

    def generate(self, prompt: str) -> bytes: ...


class TranscriptionPort(Protocol):
    """Speech-to-text for community watcher voice reports."""

    def transcribe(self, audio: bytes, language: str = "es") -> str: ...

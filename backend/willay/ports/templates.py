from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


class TemplateNotAvailable(Exception):
    """No acceptable (human-reviewed) message template exists for the requested language."""

    def __init__(self, language: str, reason: str) -> None:
        super().__init__(f"alert message not available in '{language}': {reason}")
        self.language = language
        self.reason = reason


@dataclass(frozen=True)
class ReviewedTemplates:
    """Message templates written or approved by a named human reviewer.

    `messages` maps an alert tier name (PREPARATION, ATTENTION, EVACUATION, PENDING_CONFIRMATION)
    to text with the placeholders {tier} {site_name} {distance_km} {safe_point} {official_source}
    {disclaimer}.
    """

    language: str
    reviewed_by: str
    reviewed_on: str
    messages: Mapping[str, str]


class ReviewedTemplatesPort(Protocol):
    """Source of human-reviewed templates for languages without built-in text (e.g. Quechua)."""

    def get(self, language: str) -> ReviewedTemplates | None:
        """Return the reviewed templates, None when there are none, or raise TemplateNotAvailable
        when a file exists but is not an acceptable human-reviewed template."""
        ...

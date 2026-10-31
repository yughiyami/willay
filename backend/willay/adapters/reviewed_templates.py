"""Human-reviewed message templates read from JSON files (one file per language code).

Quechua (and any other language without built-in text) is NEVER machine-generated: a language is
available only when `<dir>/<language>.json` exists and declares `"status": "human_reviewed"`
together with the reviewer's name and the review date.

File format:
    {
      "language": "qu",
      "status": "human_reviewed",
      "reviewed_by": "Name of the reviewer",
      "reviewed_on": "YYYY-MM-DD",
      "messages": {"PREPARATION": "...", "ATTENTION": "...", "EVACUATION": "...", "PENDING_CONFIRMATION": "..."}
    }
Placeholders: {tier} {site_name} {distance_km} {safe_point} {official_source} {disclaimer}
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from willay.ports.templates import ReviewedTemplates, TemplateNotAvailable

_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


class FileReviewedTemplates:
    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

    def get(self, language: str) -> ReviewedTemplates | None:
        if not _LANGUAGE_CODE.match(language):
            return None
        path = self._directory / f"{language}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TemplateNotAvailable(language, f"the template file cannot be read ({error})") from error
        problems = []
        if data.get("status") != "human_reviewed":
            problems.append("status is not 'human_reviewed'")
        if not str(data.get("reviewed_by", "")).strip():
            problems.append("reviewed_by is empty")
        if not str(data.get("reviewed_on", "")).strip():
            problems.append("reviewed_on is empty")
        if data.get("language") != language:
            problems.append("the language field does not match the file name")
        messages = data.get("messages")
        if not isinstance(messages, dict) or not messages:
            problems.append("no messages")
        if problems:
            raise TemplateNotAvailable(
                language, "the file is not an acceptable human-reviewed template: " + "; ".join(problems)
            )
        return ReviewedTemplates(
            language=language,
            reviewed_by=str(data["reviewed_by"]),
            reviewed_on=str(data["reviewed_on"]),
            messages={str(k): str(v) for k, v in messages.items()},
        )

import json

import pytest

from willay.adapters.reviewed_templates import FileReviewedTemplates
from willay.ports.templates import TemplateNotAvailable


def write(directory, language, payload):
    (directory / f"{language}.json").write_text(json.dumps(payload), encoding="utf-8")


def valid(**overrides):
    payload = {
        "language": "qu",
        "status": "human_reviewed",
        "reviewed_by": "Test Reviewer",
        "reviewed_on": "2026-01-01",
        "messages": {"EVACUATION": "PLACEHOLDER {site_name} {disclaimer}"},
    }
    payload.update(overrides)
    return payload


def test_absent_file_means_not_available(tmp_path):
    assert FileReviewedTemplates(tmp_path).get("qu") is None
    assert FileReviewedTemplates(tmp_path / "does-not-exist").get("qu") is None


def test_human_reviewed_file_is_loaded(tmp_path):
    write(tmp_path, "qu", valid())
    templates = FileReviewedTemplates(tmp_path).get("qu")
    assert templates.reviewed_by == "Test Reviewer"
    assert templates.messages["EVACUATION"].startswith("PLACEHOLDER")


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "machine_generated"},
        {"status": "draft"},
        {"reviewed_by": ""},
        {"reviewed_on": ""},
        {"language": "es"},
        {"messages": {}},
    ],
)
def test_unreviewed_or_inconsistent_files_are_refused_with_a_reason(tmp_path, overrides):
    write(tmp_path, "qu", valid(**overrides))
    with pytest.raises(TemplateNotAvailable) as error:
        FileReviewedTemplates(tmp_path).get("qu")
    assert error.value.language == "qu"
    assert "human-reviewed" in str(error.value) or "review" in str(error.value)


def test_malformed_json_is_refused(tmp_path):
    (tmp_path / "qu.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TemplateNotAvailable):
        FileReviewedTemplates(tmp_path).get("qu")


def test_language_codes_cannot_escape_the_directory(tmp_path):
    assert FileReviewedTemplates(tmp_path).get("../evil") is None

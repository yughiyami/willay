"""The verified ENFEN ingest result feeds the HazardContext (seasonal rainfall outlook) in real-data mode."""

import json

from fastapi.testclient import TestClient

from tests.data_dir_factory import build_data_dir
from willay.adapters.data_dir import DataDir
from willay.api.app import create_app
from willay.api.dependencies import real_dependencies


def write_hazard(root, **overrides):
    payload = {
        "number": "03-2017",
        "date": "2017-02-02",
        "enfen_magnitude": "weak",
        "alert_status": "alert",
        "region": "costa central",
        "seasonal_rainfall_anomaly": "above_normal",
        "flags": [],
        "verified": True,
    }
    payload.update(overrides)
    derived = root / "derived"
    derived.mkdir(exist_ok=True)
    (derived / "enfen_hazard.json").write_text(json.dumps(payload), encoding="utf-8")


def hazard(tmp_path, environ=None):
    root = build_data_dir(tmp_path)
    return root, lambda: TestClient(create_app(real_dependencies(DataDir(root), environ=environ or {}))).get("/sites").json()


def test_verified_outlook_for_the_latest_communique_sets_the_seasonal_anomaly(tmp_path):
    root, get = hazard(tmp_path)
    write_hazard(root)
    assert get()["hazard_context"]["seasonal_rainfall_anomaly"] == "above_normal"


def test_a_stale_ingest_for_another_communique_is_ignored(tmp_path):
    root, get = hazard(tmp_path)
    write_hazard(root, number="02-2017")
    assert get()["hazard_context"]["seasonal_rainfall_anomaly"] == "normal"


def test_an_unverified_ingest_is_ignored(tmp_path):
    root, get = hazard(tmp_path)
    write_hazard(root, verified=False, flags=["magnitude_mismatch"])
    assert get()["hazard_context"]["seasonal_rainfall_anomaly"] == "normal"


def test_a_null_outlook_leaves_the_default(tmp_path):
    root, get = hazard(tmp_path)
    write_hazard(root, seasonal_rainfall_anomaly=None)
    assert get()["hazard_context"]["seasonal_rainfall_anomaly"] == "normal"


def test_explicit_environment_override_wins_over_the_ingest(tmp_path):
    root, get = hazard(tmp_path, environ={"WILLAY_SEASONAL_ANOMALY": "below_normal"})
    write_hazard(root)
    assert get()["hazard_context"]["seasonal_rainfall_anomaly"] == "below_normal"


def test_the_source_citation_stays_plain_and_the_provenance_goes_to_hazard_notes(tmp_path):
    root, get = hazard(tmp_path)
    write_hazard(root)
    body = get()
    assert body["official_source"] == "ENFEN Comunicado Oficial N°03-2017 (2017-02-02)"  # cited verbatim in household messages
    assert any("rainfall outlook" in note and "above_normal" in note for note in body["hazard_notes"])


def test_hazard_notes_are_empty_without_an_ingest(tmp_path):
    _, get = hazard(tmp_path)
    assert get()["hazard_notes"] == []

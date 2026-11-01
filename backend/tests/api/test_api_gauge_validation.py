"""The public-dataset validation of the staff-gauge watcher is served to the Evidencia tab when the results file exists."""

import json

from fastapi.testclient import TestClient

from tests.data_dir_factory import build_data_dir
from willay.adapters.data_dir import DataDir
from willay.api.app import create_app
from willay.api.dependencies import real_dependencies

RESULTS = {
    "dataset": "Fang & Duan 2024 (CC BY 4.0), one urban lake in South China",
    "summary": {"coverage_pct": 97.5, "mae_cm": 4.6, "within_10cm_pct": 83.1, "baseline_mae_cm": 7.8, "baseline_within_10cm_pct": 68.0},
}


def client_for(root, with_results=True) -> TestClient:
    if with_results:
        folder = root / "derived" / "sam_gauge_validation"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "results.json").write_text(json.dumps(RESULTS), encoding="utf-8")
    return TestClient(create_app(real_dependencies(DataDir(root))))


def test_validation_endpoint_returns_the_stored_results_with_the_caveat(tmp_path):
    body = client_for(build_data_dir(tmp_path)).get("/validation/gauge").json()
    assert body["summary"]["mae_cm"] == 4.6
    assert "one site" in body["caveat"].lower() and "calibration" in body["caveat"].lower()


def test_without_the_results_file_the_endpoint_is_404(tmp_path):
    assert client_for(build_data_dir(tmp_path), with_results=False).get("/validation/gauge").status_code == 404

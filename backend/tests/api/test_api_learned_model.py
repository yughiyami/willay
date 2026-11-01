"""The learned district model is served next to the static score and the baselines (when the script has run)."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.data_dir_factory import build_data_dir
from willay.adapters.data_dir import DataDir
from willay.api.app import create_app
from willay.api.dependencies import real_dependencies

LEARNED = {
    "generated_on": "2026-10-31",
    "split": {"train_seasons": [2003, 2016], "test_seasons": [2017, 2023]},
    "model": {"kind": "logistic_regression_l2", "features": ["mm_high_share"], "coefficients": {"mm_high_share": 0.1}, "train_base_rate": 0.09},
    "evaluation": {"2017": {"learned": {"auc": 0.55, "auc_ci95": [0.41, 0.68]}}},
    "districts": {
        "150722": {"probability": 0.30, "name": "SAN MATEO"},
        "150705": {"probability": 0.10, "name": "CHICLA"},
        "150401": {"probability": 0.02, "name": "CANTA"},
    },
    "caveat": "typical-season propensity",
}


def client_for(root, with_learned=True) -> TestClient:
    if with_learned:
        (root / "derived" / "learned_model.json").write_text(json.dumps(LEARNED), encoding="utf-8")
    return TestClient(create_app(real_dependencies(DataDir(root))))


def test_districts_carry_the_learned_probability_and_its_rank(tmp_path):
    client = client_for(build_data_dir(tmp_path))
    props = {f["properties"]["ubigeo"]: f["properties"] for f in client.get("/districts").json()["features"]}
    assert props["150722"]["learned"]["probability"] == pytest.approx(0.30)
    assert props["150722"]["learned"]["percentile_rank"] > props["150401"]["learned"]["percentile_rank"]
    assert props["150722"]["learned"]["scope"] == "typical_season_propensity"


def test_learned_model_endpoint_returns_the_evaluation_and_the_caveat(tmp_path):
    client = client_for(build_data_dir(tmp_path))
    body = client.get("/learned-model").json()
    assert body["split"]["test_seasons"] == [2017, 2023]
    assert body["evaluation"]["2017"]["learned"]["auc_ci95"] == [0.41, 0.68]
    assert "districts" not in body  # the per-district table is served inside /districts
    assert "propensity" in body["caveat"]


def test_without_the_script_output_the_layer_is_simply_absent(tmp_path):
    client = client_for(build_data_dir(tmp_path), with_learned=False)
    props = client.get("/districts").json()["features"][0]["properties"]
    assert props.get("learned") is None
    assert client.get("/learned-model").status_code == 404

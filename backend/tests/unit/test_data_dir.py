from pathlib import Path

import pytest

from willay.adapters.data_dir import DataDir, DataDirError, resolve_data_dir

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_default_is_the_sibling_willay_data_folder_of_the_repo():
    assert resolve_data_dir({}) == (REPO_ROOT.parent / "willay-data").resolve()


def test_env_variable_overrides_the_default(tmp_path):
    assert resolve_data_dir({"WILLAY_DATA_DIR": str(tmp_path)}) == tmp_path.resolve()


def test_relative_env_value_is_resolved_against_the_repo_root_not_the_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir({"WILLAY_DATA_DIR": "../elsewhere"}) == (REPO_ROOT / "../elsewhere").resolve()


def test_paths_point_into_processed_raw_and_derived(tmp_path):
    data = DataDir(tmp_path)
    assert data.processed("a.csv") == tmp_path / "processed" / "a.csv"
    assert data.raw("ana", "b.geojson") == tmp_path / "raw" / "ana" / "b.geojson"
    assert data.derived("flowpaths.geojson") == tmp_path / "derived" / "flowpaths.geojson"


def test_require_reports_every_missing_file_with_a_hint(tmp_path):
    data = DataDir(tmp_path)
    (tmp_path / "processed").mkdir()
    (tmp_path / "processed" / "present.csv").write_text("x", encoding="utf-8")
    with pytest.raises(DataDirError) as error:
        data.require(data.processed("present.csv"), data.derived("flowpaths.geojson"))
    message = str(error.value)
    assert "flowpaths.geojson" in message
    assert "present.csv" not in message
    assert "WILLAY_DATA_DIR" in message


def test_derived_files_are_the_only_writable_location(tmp_path):
    data = DataDir(tmp_path)
    target = data.write_derived("out.json", '{"a": 1}')
    assert target == tmp_path / "derived" / "out.json"
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_write_derived_refuses_to_escape_the_derived_folder(tmp_path):
    with pytest.raises(DataDirError):
        DataDir(tmp_path).write_derived("../processed/evil.csv", "x")

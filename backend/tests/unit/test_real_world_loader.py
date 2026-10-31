import json

import pytest

from tests.data_dir_factory import build_data_dir
from willay.adapters.data_dir import DataDir, DataDirError
from willay.adapters.geodata.loader import (
    build_flow_path,
    load_real_world,
    susceptibility_from_class,
)
from willay.domain.entities import RiskLevel, SiteStatus


@pytest.fixture()
def world(tmp_path):
    return load_real_world(DataDir(build_data_dir(tmp_path)))


def site(world, site_id):
    return next(s for s in world.sites if s.id == site_id)


def test_susceptibility_class_maps_linearly_into_the_open_unit_interval():
    assert [susceptibility_from_class(k) for k in (1, 2, 3, 4, 5)] == pytest.approx([0.1, 0.3, 0.5, 0.7, 0.9])


def test_all_inventory_sites_are_loaded_and_marked_real_not_synthetic(world):
    assert {s.id for s in world.sites} == {"ingemmet-1", "ingemmet-2", "ingemmet-3", "ingemmet-4"}
    assert all(s.synthetic is False for s in world.sites)
    assert site(world, "ingemmet-3").in_basin is False


def test_site_with_known_attributes_has_no_quality_flags_beyond_the_data_it_lacks(world):
    alpha = site(world, "ingemmet-1")
    assert alpha.status is SiteStatus.ABANDONED
    assert alpha.declared_risk is RiskLevel.HIGH
    assert alpha.susceptibility == pytest.approx(0.7)
    assert alpha.distance_to_river_m == 120.0
    assert alpha.flow_path is not None
    assert alpha.flow_path.total_length_m == pytest.approx(22239.0)
    assert alpha.data_quality == ()


def test_unknown_attributes_and_missing_features_are_flagged_not_guessed(world):
    beta = site(world, "ingemmet-2")
    assert beta.status is SiteStatus.UNKNOWN and beta.declared_risk is RiskLevel.UNKNOWN
    assert beta.susceptibility == 0.5  # neutral default
    assert {"status_unknown", "risk_level_unknown", "susceptibility_unsampled"} <= set(beta.data_quality)


def test_site_without_a_flow_path_or_features_is_still_listed_with_flags(world):
    gamma = site(world, "ingemmet-3")
    assert gamma.flow_path is None
    assert "no_flow_path" in gamma.data_quality
    assert "no_site_features" in gamma.data_quality
    assert gamma.distance_to_river_m > 1000  # neutral far default, flagged above


def test_missing_volume_is_imputed_and_flagged(world):
    delta = site(world, "ingemmet-4")
    assert delta.volume_m3 > 0
    assert "volume_imputed_median" in delta.data_quality


def test_settlements_use_pob_rev_and_keep_the_ubigeo(world):
    by_id = {s.id: s for s in world.settlements}
    assert by_id["ana-9002"].population == 3000
    assert by_id["ana-9002"].ubigeo == "150705"


def test_intakes_are_approximate(world):
    (intake,) = world.intakes
    assert intake.approximate is True and "SEDAPAL" in intake.note


def test_secondary_layers_and_baselines_are_available(world):
    assert len(world.pasivos) == 2 and all(p.inventory_year == 2011 for p in world.pasivos)
    assert {d.ubigeo for d in world.cenepred} == {"150722", "150705", "150401"}
    assert world.site_features["ingemmet-1"]["susceptibility_label"] == "Alta"
    assert world.flow_path_meta["ingemmet-1"]["stop_reason"] == "max_length"


def test_missing_derived_files_name_the_script_that_builds_them(tmp_path):
    build_data_dir(tmp_path, with_derived=False)
    with pytest.raises(DataDirError) as error:
        load_real_world(DataDir(tmp_path))
    assert "flowpaths.geojson" in str(error.value)


def test_flow_path_is_built_from_vertices_and_cumulative_distances():
    path = build_flow_path(
        {"geometry": {"coordinates": [[-76.5, -11.8], [-76.5, -11.9]]}, "properties": {"cumulative_distance_m": [0.0, 11119.5]}}
    )
    assert [p.cumulative_distance_m for p in path.points] == [0.0, 11119.5]
    assert path.points[1].lat == -11.9


def test_flow_path_with_mismatched_distances_is_rejected():
    with pytest.raises(ValueError):
        build_flow_path(
            {"geometry": {"coordinates": [[-76.5, -11.8], [-76.5, -11.9]]}, "properties": {"cumulative_distance_m": [0.0]}}
        )


def test_loader_never_writes_into_the_data_folder(tmp_path):
    root = build_data_dir(tmp_path)
    before = sorted(str(p) for p in root.rglob("*"))
    load_real_world(DataDir(root))
    assert sorted(str(p) for p in root.rglob("*")) == before
    assert json.loads((root / "derived" / "flowpaths.geojson").read_text(encoding="utf-8"))["features"]

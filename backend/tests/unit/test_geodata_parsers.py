"""Parsing logic of the geodata adapters, on tiny synthetic rows (never the real data)."""

from datetime import date

import pytest

from willay.adapters.geodata.emergencies import (
    TARGET_HAZARDS,
    filter_emergencies,
    first_emergency_by_district,
    parse_indeci_rows,
)
from willay.adapters.geodata.hazards import (
    parse_cenepred_rows,
    parse_critical_points,
    sample_susceptibility_class,
    summarize_susceptibility_by_district,
)
from willay.adapters.geodata.intakes import parse_osm_intakes
from willay.adapters.geodata.settlements import parse_settlement_rows
from willay.adapters.geodata.tailings import (
    impute_missing_volumes,
    parse_pasivo_rows,
    parse_tailings_rows,
)
from willay.adapters.geodata.timeseries import parse_chirps_rows, parse_enfen_rows
from willay.domain.entities import EnfenAlertStatus, EnfenMagnitude, RiskLevel, SiteStatus

# --- tailings inventory -------------------------------------------------------


def tailings_row(**overrides) -> dict:
    base = {
        "lon": "-76.44",
        "lat": "-11.56",
        "in_rimac_basin": "1",
        "ID": "13061",
        "NOMBRE_PAS": "C.M. HUAMPAR S.A.",
        "SUBTIPO": "RELAVES",
        "PROVINCIA": "HUAROCHIRI",
        "DISTRITO": "HUANZA",
        "CUENCA": "CUENCA RÍMAC",
        "ESTADO": "Abandonado",
        "RIESGO": "Alto",
        "VOLUMEN": "77916.76751",
        "AREA_M2": "7791.676751",
        "PROFUNDIDAD_M": "10",
    }
    base.update(overrides)
    return base


def test_tailings_row_maps_known_status_and_risk_without_quality_flags():
    (rec,) = parse_tailings_rows([tailings_row()])
    assert rec.id == "ingemmet-13061"
    assert rec.name == "C.M. HUAMPAR S.A."
    assert rec.status is SiteStatus.ABANDONED
    assert rec.declared_risk is RiskLevel.HIGH
    assert rec.volume_m3 == pytest.approx(77916.76751)
    assert rec.area_m2 == pytest.approx(7791.676751)
    assert rec.in_basin is True
    assert rec.data_quality == ()


@pytest.mark.parametrize("blank", [" ", "", "-", "  "])
def test_blank_status_and_risk_are_unknown_and_flagged(blank):
    (rec,) = parse_tailings_rows([tailings_row(ESTADO=blank, RIESGO=blank)])
    assert rec.status is SiteStatus.UNKNOWN
    assert rec.declared_risk is RiskLevel.UNKNOWN
    assert "status_unknown" in rec.data_quality
    assert "risk_level_unknown" in rec.data_quality


@pytest.mark.parametrize(
    "raw, expected",
    [("Cerrado", SiteStatus.INACTIVE), ("ACTIVO", SiteStatus.ACTIVE), ("Abandonado", SiteStatus.ABANDONED)],
)
def test_status_vocabulary(raw, expected):
    assert parse_tailings_rows([tailings_row(ESTADO=raw)])[0].status is expected


@pytest.mark.parametrize(
    "raw, expected",
    [("Muy alto", RiskLevel.VERY_HIGH), ("Medio", RiskLevel.MEDIUM), ("bajo", RiskLevel.LOW), ("Extremo", RiskLevel.UNKNOWN)],
)
def test_risk_vocabulary_unrecognised_values_stay_unknown(raw, expected):
    assert parse_tailings_rows([tailings_row(RIESGO=raw)])[0].declared_risk is expected


def test_basin_flag_and_missing_volume_and_area():
    rows = [tailings_row(in_rimac_basin="0", VOLUMEN="", AREA_M2="")]
    (rec,) = parse_tailings_rows(rows)
    assert rec.in_basin is False
    assert rec.volume_m3 is None
    assert rec.area_m2 is None


def test_volume_is_estimated_from_area_and_depth_when_only_volume_is_missing():
    (rec,) = parse_tailings_rows([tailings_row(VOLUMEN="", AREA_M2="5000", PROFUNDIDAD_M="10")])
    assert rec.volume_m3 == pytest.approx(50_000.0)
    assert "volume_estimated_from_area" in rec.data_quality


def test_remaining_missing_volumes_get_the_median_of_known_ones_and_a_flag():
    recs = parse_tailings_rows(
        [
            tailings_row(ID="1", VOLUMEN="100", AREA_M2="10"),
            tailings_row(ID="2", VOLUMEN="300", AREA_M2="30"),
            tailings_row(ID="3", VOLUMEN="900", AREA_M2="90"),
            tailings_row(ID="4", VOLUMEN="", AREA_M2=""),
        ]
    )
    filled = impute_missing_volumes(recs)
    last = filled[-1]
    assert last.volume_m3 == 300.0
    assert "volume_imputed_median" in last.data_quality
    assert filled[0].volume_m3 == 100.0
    assert "volume_imputed_median" not in filled[0].data_quality


def test_pasivos_rows_are_flagged_as_the_2011_inventory():
    rows = [
        {
            "lon": "-76.5",
            "lat": "-12.0",
            "in_rimac_basin": "1",
            "OBJECTID": "55",
            "NOMBREDM": "MINA X",
            "TIPO": "RESIDUO MINERO",
            "SUBTIPO": "RELAVES",
            "DISTRITO": "CHICLA",
            "CUENCA": "RIMAC",
            "RESPONSABLE": "---",
        }
    ]
    (p,) = parse_pasivo_rows(rows)
    assert p.id == "minem-55"
    assert p.subtype == "RELAVES"
    assert p.inventory_year == 2011
    assert "minem_2011_inventory" in p.data_quality


# --- settlements --------------------------------------------------------------


def test_settlement_uses_pob_rev_and_never_the_corrupted_pob_column():
    rows = [
        {
            "IDCCPP": "701040001",
            "NOMBCCPP": "LA PERLA",
            "NOMDIST": "LA PERLA",
            "UBIGEO": "70104",
            "lon": "-77.108",
            "lat": "-12.0657",
            "pob_rev": "62387",
            "Pob": "1.18",
            "pob": "1.18",
        }
    ]
    (s,) = parse_settlement_rows(rows)
    assert s.population == 62387
    assert s.id == "ana-701040001"
    assert s.ubigeo == "070104"  # zero padded
    assert s.district == "LA PERLA"
    assert s.households == 0  # not published by the source; never invented


def test_settlement_without_pob_rev_is_rejected_rather_than_falling_back_to_pob():
    rows = [{"IDCCPP": "1", "NOMBCCPP": "X", "UBIGEO": "150101", "lon": "-77", "lat": "-12", "Pob": "1.18"}]
    assert parse_settlement_rows(rows) == []


# --- water intakes ------------------------------------------------------------


def osm(elements):
    return {"elements": elements}


def test_osm_water_works_and_bocatomas_become_approximate_intakes():
    data = osm(
        [
            {"type": "way", "id": 1, "center": {"lat": -12.033, "lon": -76.975}, "tags": {"man_made": "water_works", "name": "La Atarjea", "owner": "Sedapal"}},
            {"type": "way", "id": 2, "center": {"lat": -12.009, "lon": -76.881}, "tags": {"name": "Bocatoma Huachipa", "industrial": "water", "operator": "SEDAPAL"}},
            {"type": "way", "id": 3, "center": {"lat": -12.026, "lon": -76.966}, "tags": {"name": "Bocatoma La Atarjea", "waterway": "weir"}},
            {"type": "node", "id": 4, "lat": -12.0, "lon": -77.0, "tags": {"man_made": "water_works"}},
        ]
    )
    intakes = parse_osm_intakes(data)
    assert {i.id for i in intakes} == {"osm-way-1", "osm-way-2", "osm-way-3", "osm-node-4"}
    for i in intakes:
        assert i.approximate is True
        assert "SEDAPAL" in i.note
    huachipa = next(i for i in intakes if i.id == "osm-way-2")
    assert (huachipa.lon, huachipa.lat) == (-76.881, -12.009)
    assert huachipa.operator == "SEDAPAL"
    assert next(i for i in intakes if i.id == "osm-node-4").name == "Unnamed OSM water works"


def test_osm_streets_stops_and_wastewater_plants_are_not_intakes():
    data = osm(
        [
            {"type": "way", "id": 5, "center": {"lat": -12.03, "lon": -76.98}, "tags": {"highway": "residential", "name": "Calle La Bocatoma"}},
            {"type": "node", "id": 6, "lat": -12.04, "lon": -76.98, "tags": {"highway": "bus_stop", "name": "Atarjea"}},
            {"type": "way", "id": 7, "center": {"lat": -12.0, "lon": -77.0}, "tags": {"man_made": "wastewater_plant", "name": "PTAR"}},
            {"type": "way", "id": 8, "center": {"lat": -12.0, "lon": -77.0}, "tags": {"man_made": "water_tower"}},
        ]
    )
    assert parse_osm_intakes(data) == []


# --- ANA critical points ------------------------------------------------------


def test_critical_points_parse_hazards_and_location():
    gj = {
        "features": [
            {
                "geometry": {"type": "Point", "coordinates": [-76.7, -11.95]},
                "properties": {"Ubigeo": "150103", "Distrito": "Chosica", "Rio_QDA": "Rimac", "Periodo": 2017, "TiposPeligro": "Flujo de detritos, Inundación fluvial", "Peligro": "Muy Alto", "Viviendas": 120},
            },
            {"geometry": None, "properties": {"Ubigeo": "150103"}},
        ]
    }
    (cp,) = parse_critical_points(gj)
    assert (cp.lon, cp.lat) == (-76.7, -11.95)
    assert cp.ubigeo == "150103"
    assert cp.period == 2017
    assert cp.hazard_level == "Muy Alto"
    assert cp.homes == 120
    assert "Flujo de detritos" in cp.hazard_types


# --- CENEPRED -----------------------------------------------------------------


def test_cenepred_rows_keep_scenario_values_per_district():
    rows = [
        {
            "ubigeo": "150118",
            "dep": "LIMA",
            "prov": "LIMA",
            "dist": "LURIGANCHO",
            "pob_2017": "240814",
            "viv_2017": "70000",
            "flood_riesgo_val": "3.925",
            "flood_riesgo_nivel": "Alto",
            "mm_riesgo_val": "2.5",
            "mm_riesgo_nivel": "Alto",
            "area_km2": "236.5",
        }
    ]
    (d,) = parse_cenepred_rows(rows)
    assert d.ubigeo == "150118"
    assert d.name == "LURIGANCHO"
    assert d.population_2017 == 240814
    assert d.flood_risk_value == pytest.approx(3.925)
    assert d.mm_risk_level == "Alto"
    assert d.area_km2 == pytest.approx(236.5)


# --- INGEMMET susceptibility --------------------------------------------------


def square(x0, y0, x1, y1):
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


def susceptibility_feature(coords, grid, cd="150107", area=1.0, kind="Polygon"):
    return {
        "geometry": {"type": kind, "coordinates": coords},
        "properties": {"GRIDCODE": grid, "CD_DIST": cd, "AREA_KM2": area},
    }


def test_sampling_returns_the_class_of_the_polygon_containing_the_point():
    features = [
        susceptibility_feature(square(0, 0, 1, 1), 2),
        susceptibility_feature(square(1, 0, 2, 1), 5),
    ]
    assert sample_susceptibility_class(1.5, 0.5, features) == 5
    assert sample_susceptibility_class(0.5, 0.5, features) == 2


def test_sampling_outside_all_polygons_or_in_a_hole_returns_none():
    ring_with_hole = square(0, 0, 4, 4) + square(1, 1, 3, 3)
    features = [susceptibility_feature(ring_with_hole, 4)]
    assert sample_susceptibility_class(2, 2, features) is None  # inside the hole
    assert sample_susceptibility_class(0.5, 0.5, features) == 4
    assert sample_susceptibility_class(10, 10, features) is None


def test_sampling_handles_multipolygons_and_overlaps_take_the_highest_class():
    multi = susceptibility_feature([square(0, 0, 1, 1), square(5, 5, 6, 6)], 3, kind="MultiPolygon")
    overlap = susceptibility_feature(square(0, 0, 1, 1), 5)
    assert sample_susceptibility_class(5.5, 5.5, [multi]) == 3
    assert sample_susceptibility_class(0.5, 0.5, [multi, overlap]) == 5


def test_class_zero_polygons_are_not_a_valid_sample():
    assert sample_susceptibility_class(0.5, 0.5, [susceptibility_feature(square(0, 0, 1, 1), 0)]) is None


def test_high_susceptibility_area_share_by_district():
    features = [
        susceptibility_feature(square(0, 0, 1, 1), 5, cd="150107", area=3.0),
        susceptibility_feature(square(0, 0, 1, 1), 4, cd="150107", area=1.0),
        susceptibility_feature(square(0, 0, 1, 1), 2, cd="150107", area=4.0),
        susceptibility_feature(square(0, 0, 1, 1), 1, cd="150108", area=2.0),
        susceptibility_feature(square(0, 0, 1, 1), 0, cd="150108", area=0.0),
        susceptibility_feature(square(0, 0, 1, 1), 3, cd=" ", area=9.0),
    ]
    summary = summarize_susceptibility_by_district(features)
    assert set(summary) == {"150107", "150108"}
    assert summary["150107"].total_area_km2 == pytest.approx(8.0)
    assert summary["150107"].high_area_km2 == pytest.approx(4.0)
    assert summary["150107"].high_share == pytest.approx(0.5)
    assert summary["150108"].high_share == 0.0


# --- INDECI -------------------------------------------------------------------


def indeci_row(**overrides) -> dict:
    base = {"fecha": "2017-03-15", "ubigeo": "150103", "dist": "CHOSICA", "emergencia": "HUAYCO"}
    base.update(overrides)
    return base


def test_target_hazards_are_the_rain_driven_types():
    assert set(TARGET_HAZARDS) == {
        "HUAYCO",
        "FLUJO DE DETRITOS",
        "DESLIZAMIENTO",
        "INUNDACION",
        "LLUVIA INTENSA",
    }


def test_indeci_rows_parse_dates_and_pad_ubigeo_and_ignore_bad_rows():
    rows = [indeci_row(ubigeo="70101", fecha="2017-01-02"), indeci_row(fecha="not a date"), indeci_row(ubigeo="")]
    emergencies = parse_indeci_rows(rows)
    assert len(emergencies) == 1
    assert emergencies[0].ubigeo == "070101"
    assert emergencies[0].date == date(2017, 1, 2)


def test_filter_keeps_target_hazards_inside_the_window_and_matches_accents_case_insensitively():
    emergencies = parse_indeci_rows(
        [
            indeci_row(emergencia="HUAYCO", fecha="2017-03-15"),
            indeci_row(emergencia="InundaciÓn", fecha="2017-01-01"),
            indeci_row(emergencia="INCENDIO URB. E INDUST.", fecha="2017-03-15"),
            indeci_row(emergencia="DERRUMBE DE CERRO", fecha="2017-03-15"),
            indeci_row(emergencia="LLUVIA INTENSA", fecha="2017-05-01"),
            indeci_row(emergencia="HUAYCO", fecha="2016-12-31"),
        ]
    )
    kept = filter_emergencies(emergencies, TARGET_HAZARDS, date(2017, 1, 1), date(2017, 4, 30))
    assert sorted(e.hazard for e in kept) == ["HUAYCO", "INUNDACION"]


def test_first_emergency_per_district():
    emergencies = parse_indeci_rows(
        [
            indeci_row(ubigeo="150103", fecha="2017-03-16"),
            indeci_row(ubigeo="150103", fecha="2017-03-14"),
            indeci_row(ubigeo="150118", fecha="2017-02-01"),
        ]
    )
    first = first_emergency_by_district(emergencies)
    assert first == {"150103": date(2017, 3, 14), "150118": date(2017, 2, 1)}


# --- CHIRPS and ENFEN ---------------------------------------------------------


def test_chirps_rows_are_parsed_in_date_order():
    rows = [
        {"date": "2017-01-02", "basin_mean_mm": "0.36", "basin_max_cell_mm": "3.26", "sierra_mean_mm": "0.57", "bbox_mean_mm": "0.74"},
        {"date": "2017-01-01", "basin_mean_mm": "0.44", "basin_max_cell_mm": "4.35", "sierra_mean_mm": "0.48", "bbox_mean_mm": "0.83"},
    ]
    days = parse_chirps_rows(rows)
    assert [d.date for d in days] == [date(2017, 1, 1), date(2017, 1, 2)]
    assert days[0].sierra_mean_mm == 0.48


def enfen_row(d, status, text, number="01-2017"):
    return {"date": d, "number": number, "alert_status": status, "magnitude_forecast": text}


def test_enfen_status_is_normalised():
    rows = [
        enfen_row("2017-01-16", "No Activo", "Nino 1+2: neutral 66%, weak coastal El Nino 30% for summer"),
        enfen_row("2017-01-24", "Vigilancia de El Nino costero", "weak coastal El Nino expected this summer"),
        enfen_row("2017-02-02", "Alerta de El Nino costero", "weak; conditions favour more frequent very strong rainfall"),
    ]
    bulletins = parse_enfen_rows(rows)
    assert [b.alert_status for b in bulletins] == [
        EnfenAlertStatus.NOT_ACTIVE,
        EnfenAlertStatus.WATCH,
        EnfenAlertStatus.ALERT,
    ]


def test_enfen_magnitude_is_the_first_one_mentioned_and_neutral_when_not_active():
    rows = [
        enfen_row("2017-01-16", "No Activo", "Nino 1+2: neutral 66%, weak coastal El Nino 30%"),
        enfen_row("2017-03-02", "Alerta de El Nino costero", "weak to moderate, at least through April"),
        enfen_row("2023-04-13", "Alerta de El Nino costero", "moderate until winter, strong not ruled out in April"),
        enfen_row("2023-05-12", "Alerta de El Nino costero", "strong to moderate until early spring"),
    ]
    mags = [b.magnitude for b in parse_enfen_rows(rows)]
    assert mags == [EnfenMagnitude.NEUTRAL, EnfenMagnitude.WEAK, EnfenMagnitude.MODERATE, EnfenMagnitude.STRONG]


def test_enfen_bulletin_without_a_magnitude_keyword_inherits_the_previous_one():
    rows = [
        enfen_row("2017-03-30", "Alerta de El Nino costero", "moderate, at least through May"),
        enfen_row("2017-04-20", "Alerta de El Nino costero", "continues through at least May, lower intensity than summer"),
        enfen_row("2017-06-12", "No Activo", "event ended in May; neutral conditions expected"),
    ]
    mags = [b.magnitude for b in parse_enfen_rows(rows)]
    assert mags == [EnfenMagnitude.MODERATE, EnfenMagnitude.MODERATE, EnfenMagnitude.NEUTRAL]


def test_enfen_bulletins_are_sorted_by_date():
    rows = [
        enfen_row("2017-02-02", "Alerta de El Nino costero", "weak"),
        enfen_row("2017-01-24", "Vigilancia de El Nino costero", "weak"),
    ]
    assert [b.date for b in parse_enfen_rows(rows)] == [date(2017, 1, 24), date(2017, 2, 2)]


def test_susceptibility_area_prefers_shape_area_because_area_km2_is_rounded_to_integers():
    one_km2_in_deg2 = 1.0 / (111.32 * 0.9786 * 110.574)  # about 8.3e-5 deg2 at latitude -11.9
    features = [
        {
            "geometry": None,
            "properties": {"GRIDCODE": 5, "CD_DIST": "150107", "AREA_KM2": 0, "SHAPE.AREA": 3 * one_km2_in_deg2},
        },
        {
            "geometry": None,
            "properties": {"GRIDCODE": 2, "CD_DIST": "150107", "AREA_KM2": 0, "SHAPE.AREA": 1 * one_km2_in_deg2},
        },
    ]
    summary = summarize_susceptibility_by_district(features)["150107"]
    assert summary.high_share == pytest.approx(0.75, rel=0.01)
    assert summary.total_area_km2 == pytest.approx(4.0, rel=0.02)

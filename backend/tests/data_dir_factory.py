"""Builds a tiny, fully synthetic stand-in for the willay-data folder (never real data)."""

from __future__ import annotations

import json
from pathlib import Path

RELAVES_CSV = (
    "lon,lat,in_rimac_basin,ID,NOMBRE_PAS,SUBTIPO,REGION,PROVINCIA,DISTRITO,CUENCA,SUBCUENCA,MICROCUENC,"
    "ESTADO,RIESGO,IMPACTO,CONTAMINAC,VOLUMEN,AREA_M2,PROFUNDIDAD_M,ELEMENTOS,RESPONSABL,ESTUDIO_AM,LINK\n"
    "-76.50,-11.80,1,1,TEST ALPHA,RELAVES,LIMA,HUAROCHIRI,SAN MATEO,CUENCA RIMAC,X,Y,Abandonado,Alto,Alto,,"
    "100000,10000,10,,MINEM,-,\n"
    "-76.50,-11.82,1,2,TEST BETA,RELAVES,LIMA,HUAROCHIRI,CHICLA,CUENCA RIMAC,X,Y, , , , ,"
    "50000,5000,10,,-,-,\n"
    "-76.20,-11.50,0,3,TEST GAMMA,RELAVES,LIMA,CANTA,HUAMANTANGA,CUENCA CHILLON,X,Y, , , , ,"
    "20000,2000,10,,-,-,\n"
    "-76.55,-11.85,1,4,TEST DELTA,RELAVES,LIMA,HUAROCHIRI,MATUCANA,CUENCA RIMAC,X,Y, , , , , , , ,,-,-,\n"
)

PASIVOS_CSV = (
    "lon,lat,in_rimac_basin,OBJECTID,NOMBREDM,CODIGODM,TIPO,SUBTIPO,REGION,PROVINCIA,DISTRITO,CUENCA,TITULARDM,RESPONSABLE\n"
    "-76.5,-11.9,1,10,MINA UNO,x,RESIDUO MINERO,RELAVES,LIMA,HUAROCHIRI,CHICLA,RIMAC,x,---\n"
    "-76.6,-11.9,1,11,MINA DOS,x,LABOR MINERA,BOCAMINA,LIMA,HUAROCHIRI,CHICLA,RIMAC,x,---\n"
)

CCPP_CSV = (
    "IDCCPP,NOMBCCPP,NOMDIST,NOMPROV,NOMDEP,UBIGEO,lon,lat,pob_rev,altitud_zona\n"
    "9001,VILLA UNO,SAN MATEO,HUAROCHIRI,LIMA,150722,-76.50,-11.90,800,Cuenca Alta\n"
    "9002,VILLA DOS,CHICLA,HUAROCHIRI,LIMA,150705,-76.50,-12.00,3000,Cuenca Media\n"
    "9003,VILLA LEJOS,CANTA,CANTA,LIMA,150401,-76.00,-11.00,500,Cuenca Alta\n"
)

CENEPRED_CSV = (
    "ubigeo,dep,prov,dist,pob_2017,viv_2017,flood_susc_score_v_sinu_pp,flood_nivel_vulnerab,flood_riesgo_val,"
    "flood_riesgo_nivel,mm_nivel_peligro,mm_viv_susc_media_alta_vivsm_ma_a,mm_riesgo_val,mm_riesgo_nivel,area_km2\n"
    "150722,LIMA,HUAROCHIRI,SAN MATEO,5000,1500,3,Alto,3.0,Alto,Alto,100,3.5,Alto,300\n"
    "150705,LIMA,HUAROCHIRI,CHICLA,8000,2000,2,Medio,2.0,Medio,Medio,50,2.0,Medio,400\n"
    "150401,LIMA,CANTA,CANTA,3000,900,1,Bajo,1.0,Bajo,Bajo,10,1.0,Bajo,500\n"
)

INDECI_CSV = (
    "cod_emergencia,fecha,anio,mes,ubigeo,dpto,prov,dist,emergencia\n"
    "1,2017-03-15,2017,MARZO,150722,LIMA,HUAROCHIRI,SAN MATEO,HUAYCO\n"
    "2,2017-03-16,2017,MARZO,150722,LIMA,HUAROCHIRI,SAN MATEO,INUNDACION\n"
    "3,2017-03-20,2017,MARZO,150705,LIMA,HUAROCHIRI,CHICLA,INCENDIO URB. E INDUST.\n"
    "4,2023-03-14,2023,MARZO,150705,LIMA,HUAROCHIRI,CHICLA,LLUVIA INTENSA\n"
)

CHIRPS_CSV = (
    "date,basin_mean_mm,basin_max_cell_mm,sierra_mean_mm,bbox_mean_mm,n_cells_basin,n_cells_sierra\n"
    "2017-03-14,2.0,8.0,3.0,1.0,114,70\n"
    "2017-03-15,2.5,9.0,4.0,1.0,114,70\n"
)

ENFEN_CSV = (
    "date,number,alert_status,magnitude_forecast,magnitude_raw_extract,source,pdf\n"
    "2017-01-24,02-2017,Vigilancia de El Nino costero,weak coastal El Nino expected this summer,,x,y\n"
    "2017-02-02,03-2017,Alerta de El Nino costero,weak,,x,y\n"
)

OSM_JSON = {
    "elements": [
        {
            "type": "way",
            "id": 1,
            "center": {"lat": -12.0, "lon": -76.50},
            "tags": {"man_made": "water_works", "name": "Planta Test", "owner": "Sedapal"},
        }
    ]
}

CRITICAL_POINTS = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [-76.50, -11.95]},
            "properties": {"Ubigeo": "150722", "Distrito": "San Mateo", "Periodo": 2019, "Peligro": "Muy Alto"},
        }
    ]
}

FLOWPATHS = {
    "type": "FeatureCollection",
    "properties": {"method": "test"},
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[-76.50, -11.80], [-76.50, -11.90], [-76.50, -12.00]]},
            "properties": {
                "site_id": "ingemmet-1",
                "name": "TEST ALPHA",
                "length_m": 22239.0,
                "stop_reason": "max_length",
                "breached_cells": 3,
                "cumulative_distance_m": [0.0, 11119.5, 22239.0],
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[-76.50, -11.82], [-76.50, -12.00]]},
            "properties": {
                "site_id": "ingemmet-2",
                "name": "TEST BETA",
                "length_m": 20015.0,
                "stop_reason": "edge",
                "breached_cells": 0,
                "cumulative_distance_m": [0.0, 20015.0],
            },
        },
    ],
}

SITE_FEATURES = {
    "sources": {},
    "sites": {
        "ingemmet-1": {
            "susceptibility_class": 4,
            "susceptibility_label": "Alta",
            "susceptibility_method": "point_in_polygon",
            "distance_to_river_m": 120.0,
            "nearest_river": "Quebrada Test",
        },
        "ingemmet-2": {
            "susceptibility_class": None,
            "susceptibility_label": None,
            "susceptibility_method": "none",
            "distance_to_river_m": 2500.0,
            "nearest_river": "Quebrada Test",
        },
    },
}

DISTRICT_FEATURES = {
    "definition": {},
    "districts": {
        "150722": {
            "name": "SAN MATEO",
            "mm_high_share": 0.6,
            "mm_total_area_km2": 300.0,
            "mean_slope_deg": 27.0,
            "exposed_population": {"2017": 800, "2023": 800},
            "tailings_upstream": 2,
            "tailings_upstream_ids": ["ingemmet-1", "ingemmet-2"],
            "tailings_upstream_volume_m3": 150000.0,
        },
        "150705": {
            "name": "CHICLA",
            "mm_high_share": 0.3,
            "mm_total_area_km2": 400.0,
            "mean_slope_deg": 26.0,
            "exposed_population": {"2017": 3000, "2023": 3000},
            "tailings_upstream": 1,
            "tailings_upstream_ids": ["ingemmet-2"],
            "tailings_upstream_volume_m3": 50000.0,
        },
        "150401": {
            "name": "CANTA",
            "mm_high_share": 0.1,
            "mm_total_area_km2": 500.0,
            "mean_slope_deg": 22.0,
            "exposed_population": {"2017": 0, "2023": 0},
            "tailings_upstream": 0,
            "tailings_upstream_ids": [],
            "tailings_upstream_volume_m3": 0.0,
        },
    },
}


def _square(lon0, lat0, lon1, lat1):
    return {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


DISTRICT_POLYGONS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": _square(-76.6, -11.9, -76.4, -11.7), "properties": {"ubigeo": "150722", "name": "SAN MATEO"}},
        {"type": "Feature", "geometry": _square(-76.6, -12.1, -76.4, -11.9), "properties": {"ubigeo": "150705", "name": "CHICLA"}},
        {"type": "Feature", "geometry": _square(-76.1, -11.1, -75.9, -10.9), "properties": {"ubigeo": "150401", "name": "CANTA"}},
    ],
}

_METRICS = {"auc": 0.8, "auc_ci95": [0.5, 1.0], "top_k_capture": {"10": 0.5, "20": 0.5}, "max_top_k_capture": {"10": 0.5, "20": 1.0},
            "top_k_precision": {"10": 1.0, "20": 1.0}, "top_k_slots": {"10": 1, "20": 1}, "n_districts": 3}


def _event(key):
    return {
        "event": key,
        "n_districts": 3,
        "n_positive": 1,
        "base_rate": 1 / 3,
        "primary_gate": "weak_rain80",
        "models": {
            "willay_static": dict(_METRICS),
            "cenepred_baseline": dict(_METRICS),
            "population_baseline": dict(_METRICS),
        },
        "lead_time": {"weak_rain80": {"willay_static": {"values": [10], "n": 1, "median": 10.0, "p25": 10.0, "p75": 10.0, "n_without_lead": 0}}},
        "gates": {"weak_rain80": {"primary": True, "first_eligible_day": "2017-02-13"}},
        "districts": [
            {"ubigeo": "150722", "name": "SAN MATEO", "population": 5000, "emergency": True, "first_emergency": "2017-03-15",
             "scores": {"willay_static": 0.9, "cenepred_baseline": 3.2, "population_baseline": 5000.0}},
            {"ubigeo": "150705", "name": "CHICLA", "population": 8000, "emergency": False, "first_emergency": None,
             "scores": {"willay_static": 0.5, "cenepred_baseline": 2.0, "population_baseline": 8000.0}},
            {"ubigeo": "150401", "name": "CANTA", "population": 3000, "emergency": False, "first_emergency": None,
             "scores": {"willay_static": 0.1, "cenepred_baseline": 1.0, "population_baseline": 3000.0}},
        ],
    }


BACKTEST_RESULTS = {"generated_on": "2026-10-31", "primary_gate": "weak_rain80", "events": {"2017": _event("2017"), "2023": _event("2023")}}


def build_data_dir(root: Path, *, with_derived: bool = True) -> Path:
    processed = root / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    files = {
        "relaves_inventario_ingemmet_bbox.csv": RELAVES_CSV,
        "pasivos_ambientales_minem_bbox.csv": PASIVOS_CSV,
        "ccpp_cuenca_rimac_pob.csv": CCPP_CSV,
        "cenepred_er_fen2027_distritos_bbox.csv": CENEPRED_CSV,
        "indeci_emergencias_distritos_bbox_2003_2023.csv": INDECI_CSV,
        "chirps_v2_rimac_daily_mm.csv": CHIRPS_CSV,
        "enfen_timeline.csv": ENFEN_CSV,
    }
    for name, text in files.items():
        (processed / name).write_text(text, encoding="utf-8")
    (root / "raw" / "osm").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "osm" / "overpass_water_infrastructure_bbox.json").write_text(json.dumps(OSM_JSON), encoding="utf-8")
    (root / "raw" / "ana").mkdir(parents=True, exist_ok=True)
    (root / "raw" / "ana" / "puntos_criticos_2016_2024_bbox.geojson").write_text(
        json.dumps(CRITICAL_POINTS), encoding="utf-8"
    )
    if with_derived:
        derived = root / "derived"
        derived.mkdir(parents=True, exist_ok=True)
        (derived / "flowpaths.geojson").write_text(json.dumps(FLOWPATHS), encoding="utf-8")
        (derived / "site_features.json").write_text(json.dumps(SITE_FEATURES), encoding="utf-8")
        (derived / "district_features.json").write_text(json.dumps(DISTRICT_FEATURES), encoding="utf-8")
        (derived / "district_polygons.geojson").write_text(json.dumps(DISTRICT_POLYGONS), encoding="utf-8")
        (derived / "backtest_results.json").write_text(json.dumps(BACKTEST_RESULTS), encoding="utf-8")
    return root

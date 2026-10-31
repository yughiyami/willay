"""ENFEN communique ingestion: the model locates and quotes, deterministic code decides what is used."""

import base64
from datetime import date

import pytest

from willay.application.enfen_ingest import (
    ENFEN_SCHEMA,
    EnfenExtraction,
    InvalidExtraction,
    extract_enfen,
    hazard_inputs_from,
    outlook_from_quote,
    parse_extraction,
    reconcile,
)
from willay.domain.entities import EnfenAlertStatus, EnfenBulletin, EnfenMagnitude, RainfallAnomaly

TEXT_18 = (
    "Lluvias: Para lo que resta del 2026, se irán incrementando las lluvias por encima de lo normal en la "
    "costa norte, costa central y la sierra norte occidental. Para el verano de 2027, lluvias sobre lo normal "
    "en la costa norte, costa central y sierra norte occidental; y de normal a inferior en la sierra sur."
)

GOOD = {
    "number": "18-2026",
    "date": "2026-10-16",
    "alert_status": "alert",
    "nino12_forecast": [
        {"month": "2026-10", "magnitude": "extraordinary"},
        {"month": "2027-01", "magnitude": "extraordinary"},
        {"month": "2027-02", "magnitude": "strong"},
    ],
    "nino34": {"magnitude": "very_strong", "until_month": "2027-02"},
    "rain_outlook": [
        {
            "region": "costa central",
            "period": "next_summer",
            "outlook": "above_normal",
            "evidence_quote": "Para el verano de 2027, lluvias sobre lo normal en la costa norte, costa central",
        },
        {
            "region": "sierra sur",
            "period": "next_summer",
            "outlook": "below_normal",
            "evidence_quote": "de normal a inferior en la sierra sur",
        },
    ],
}


def bulletin(number="18-2026", when=date(2026, 10, 16), status=EnfenAlertStatus.ALERT, magnitude=EnfenMagnitude.EXTRAORDINARY):
    return EnfenBulletin(date=when, number=number, alert_status=status, magnitude=magnitude, source_text="x")


class FakeExtractor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def extract_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# --- extraction call ---------------------------------------------------------------------------


def test_extract_enfen_sends_the_pdf_as_input_file_with_the_schema():
    extractor = FakeExtractor(GOOD)
    result = extract_enfen(b"%PDF-1.4 fake", "enfen_2026_18.pdf", extractor)
    assert isinstance(result, EnfenExtraction)
    (call,) = extractor.calls
    assert call["schema"] == ENFEN_SCHEMA
    assert call["purpose"] == "enfen_extract"
    file_part = next(p for p in call["content"] if p["type"] == "input_file")
    assert file_part["filename"] == "enfen_2026_18.pdf"
    assert file_part["file_data"] == "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()
    assert "verbatim" in call["instructions"].lower()


def test_schema_requires_the_documented_fields():
    assert set(ENFEN_SCHEMA["required"]) == {"number", "date", "alert_status", "nino12_forecast", "nino34", "rain_outlook"}
    assert ENFEN_SCHEMA["additionalProperties"] is False


# --- parsing ---------------------------------------------------------------------------------


def test_parse_extraction_builds_typed_values():
    ex = parse_extraction(GOOD)
    assert ex.number == "18-2026" and ex.date == date(2026, 10, 16)
    assert ex.alert_status is EnfenAlertStatus.ALERT
    assert ex.nino12_forecast[0] == ("2026-10", EnfenMagnitude.EXTRAORDINARY)
    assert ex.current_magnitude is EnfenMagnitude.EXTRAORDINARY
    assert ex.nino34_magnitude == "very_strong"


@pytest.mark.parametrize(
    "patch",
    [
        {"alert_status": "red"},
        {"date": "16 octubre 2026"},
        {"nino12_forecast": []},
        {"nino12_forecast": [{"month": "2026-10", "magnitude": "huge"}]},
        {"number": ""},
    ],
)
def test_parse_extraction_rejects_malformed_values(patch):
    with pytest.raises(InvalidExtraction):
        parse_extraction({**GOOD, **patch})


# --- deterministic rain classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("lluvias sobre lo normal en la costa norte", RainfallAnomaly.ABOVE_NORMAL),
        ("las lluvias por encima de lo normal", RainfallAnomaly.ABOVE_NORMAL),
        ("de normal a inferior en la sierra sur", RainfallAnomaly.BELOW_NORMAL),
        ("precipitaciones inferiores a lo normal", RainfallAnomaly.BELOW_NORMAL),
        ("lluvias normales en la costa", RainfallAnomaly.NORMAL),
        ("lluvias sobre lo normal en la costa; y de normal a inferior en la sierra sur", None),  # mixed: ambiguous
        ("temperaturas muy superiores", None),
    ],
)
def test_outlook_from_quote(quote, expected):
    assert outlook_from_quote(quote) is expected


# --- reconciliation with the curated timeline ----------------------------------------------------------


def test_matching_extraction_is_verified_without_flags():
    rec = reconcile(parse_extraction(GOOD), [bulletin()], source_text=TEXT_18)
    assert rec.verified is True
    assert rec.flags == ()
    assert rec.magnitude is EnfenMagnitude.EXTRAORDINARY


def test_magnitude_mismatch_makes_the_timeline_win_and_raises_a_flag():
    ex = parse_extraction({**GOOD, "nino12_forecast": [{"month": "2026-10", "magnitude": "strong"}]})
    rec = reconcile(ex, [bulletin()], source_text=TEXT_18)
    assert rec.magnitude is EnfenMagnitude.EXTRAORDINARY  # human-curated CSV value
    assert "magnitude_mismatch" in rec.flags
    assert rec.verified is False


def test_date_and_status_mismatches_are_flagged_and_csv_values_are_kept():
    ex = parse_extraction({**GOOD, "date": "2027-09-28", "alert_status": "watch"})
    rec = reconcile(ex, [bulletin()], source_text=TEXT_18)
    assert {"date_mismatch", "status_mismatch"} <= set(rec.flags)
    assert rec.date == date(2026, 10, 16) and rec.alert_status is EnfenAlertStatus.ALERT


def test_a_communique_missing_from_the_timeline_is_never_used():
    ex = parse_extraction({**GOOD, "number": "19-2026", "date": "2026-10-30"})
    rec = reconcile(ex, [bulletin()], source_text=TEXT_18)
    assert rec.verified is False
    assert "not_in_timeline" in rec.flags
    assert rec.magnitude is None and rec.rain_by_region == {}


def test_rain_label_disagreeing_with_the_quote_loses_to_the_deterministic_reading():
    bad = {**GOOD, "rain_outlook": [{**GOOD["rain_outlook"][0], "outlook": "below_normal"}]}
    rec = reconcile(parse_extraction(bad), [bulletin()], source_text=TEXT_18)
    assert "rain_outlook_label_mismatch:costa central" in rec.flags
    assert rec.rain_by_region["costa central"] is RainfallAnomaly.ABOVE_NORMAL


def test_rain_quote_not_in_the_source_text_is_unverified():
    bad = {**GOOD, "rain_outlook": [{**GOOD["rain_outlook"][0], "evidence_quote": "lluvias extremas inventadas"}]}
    rec = reconcile(parse_extraction(bad), [bulletin()], source_text=TEXT_18)
    assert "rain_quote_not_found:costa central" in rec.flags
    assert "costa central" not in rec.rain_by_region


def test_without_source_text_rain_is_unverified():
    rec = reconcile(parse_extraction(GOOD), [bulletin()], source_text=None)
    assert "source_text_unavailable" in rec.flags
    assert rec.rain_by_region == {}


def test_quote_matching_tolerates_accents_and_encoding_damage():
    damaged = TEXT_18.replace("í", "�").replace("ó", "�")
    good = {**GOOD, "rain_outlook": [{**GOOD["rain_outlook"][0], "evidence_quote": "Para el verano de 2027, lluvias sobre lo normal"}]}
    rec = reconcile(parse_extraction(good), [bulletin()], source_text=damaged)
    assert rec.rain_by_region["costa central"] is RainfallAnomaly.ABOVE_NORMAL


# --- feeding the HazardContext ---------------------------------------------------------------------------


def test_hazard_inputs_use_the_next_summer_outlook_of_costa_central():
    rec = reconcile(parse_extraction(GOOD), [bulletin()], source_text=TEXT_18)
    inputs = hazard_inputs_from(rec, region="costa central")
    assert inputs.enfen_magnitude is EnfenMagnitude.EXTRAORDINARY
    assert inputs.seasonal_rainfall_anomaly is RainfallAnomaly.ABOVE_NORMAL
    assert inputs.number == "18-2026"


def test_unverified_reconciliation_yields_no_hazard_inputs():
    ex = parse_extraction({**GOOD, "number": "19-2026"})
    rec = reconcile(ex, [bulletin()], source_text=TEXT_18)
    assert hazard_inputs_from(rec, region="costa central") is None


def test_flags_do_not_block_use_of_curated_values_when_the_number_matches():
    # A rain flag leaves the curated magnitude usable but no seasonal anomaly is supplied.
    bad = {**GOOD, "rain_outlook": [{**GOOD["rain_outlook"][0], "evidence_quote": "inventado"}]}
    rec = reconcile(parse_extraction(bad), [bulletin()], source_text=TEXT_18)
    inputs = hazard_inputs_from(rec, region="costa central")
    assert inputs is not None and inputs.seasonal_rainfall_anomaly is None

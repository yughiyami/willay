"""Use case: ingest an ENFEN communique PDF with a schema-constrained model, then verify it deterministically.

The model only LOCATES and QUOTES what the official document says. What is actually used downstream is
decided by deterministic code:

* number, date, alert status and the current Nino 1+2 magnitude are compared with the human-curated
  `enfen_timeline.csv`; on any mismatch the curated value wins and a flag is raised;
* a communique that is not in the curated timeline yet is never used (it needs a human to add it);
* each rainfall outlook must carry a verbatim quote found in the document text; the outlook category is
  re-derived from the quote by fixed rules and the model's label is only kept when the rules agree.

Nothing here decides an alert tier: the result feeds `HazardContext` inputs that the domain rules evaluate.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from willay.domain.entities import EnfenAlertStatus, EnfenBulletin, EnfenMagnitude, RainfallAnomaly
from willay.ports.agent import StructuredExtractionPort

_STATUS = {"not_active": EnfenAlertStatus.NOT_ACTIVE, "watch": EnfenAlertStatus.WATCH, "alert": EnfenAlertStatus.ALERT}
_OUTLOOK = {
    "above_normal": RainfallAnomaly.ABOVE_NORMAL,
    "normal": RainfallAnomaly.NORMAL,
    "below_normal": RainfallAnomaly.BELOW_NORMAL,
}
_PERIODS = ("next_summer", "rest_of_year")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

ENFEN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "number": {"type": "string", "description": "Communique number as NN-YYYY, e.g. 18-2026"},
        "date": {"type": "string", "description": "Date in the document header as ISO YYYY-MM-DD"},
        "alert_status": {"type": "string", "enum": ["not_active", "watch", "alert"], "description": "No Activo / Vigilancia / Alerta"},
        "nino12_forecast": {
            "type": "array",
            "description": "Forecast coastal El Nino (Nino 1+2, ICEN) magnitude by month, from the current month onwards",
            "items": {
                "type": "object",
                "properties": {
                    "month": {"type": "string", "description": "YYYY-MM"},
                    "magnitude": {"type": "string", "enum": ["neutral", "weak", "moderate", "strong", "extraordinary"]},
                },
                "required": ["month", "magnitude"],
                "additionalProperties": False,
            },
        },
        "nino34": {
            "type": "object",
            "properties": {
                "magnitude": {"type": "string", "enum": ["neutral", "weak", "moderate", "strong", "very_strong"]},
                "until_month": {"type": "string", "description": "YYYY-MM or empty when not stated"},
            },
            "required": ["magnitude", "until_month"],
            "additionalProperties": False,
        },
        "rain_outlook": {
            "type": "array",
            "description": "Rainfall outlook per region and period, each with a verbatim quote from the document",
            "items": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "Region as written, lower case, e.g. costa central"},
                    "period": {"type": "string", "enum": ["rest_of_year", "next_summer"]},
                    "outlook": {"type": "string", "enum": ["above_normal", "normal", "below_normal"]},
                    "evidence_quote": {"type": "string", "description": "Verbatim sentence fragment (Spanish) supporting the outlook"},
                },
                "required": ["region", "period", "outlook", "evidence_quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["number", "date", "alert_status", "nino12_forecast", "nino34", "rain_outlook"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "You read an official ENFEN communique (Peru, Spanish) and fill the JSON schema. Use only what the document states. "
    "Copy evidence_quote verbatim from the document; never paraphrase or translate it. Report one rain_outlook entry per "
    "region and period the document names (rest_of_year = what remains of the current year; next_summer = the coming "
    "summer). Expand the Nino 1+2 magnitude into months from the current month to the last month the text covers. "
    "Do not infer alert levels or add recommendations."
)


class InvalidExtraction(ValueError):
    """The model's JSON does not satisfy the extraction contract."""


@dataclass(frozen=True)
class RainEntry:
    region: str
    period: str
    outlook: RainfallAnomaly
    quote: str


@dataclass(frozen=True)
class EnfenExtraction:
    number: str
    date: date
    alert_status: EnfenAlertStatus
    nino12_forecast: tuple[tuple[str, EnfenMagnitude], ...]
    nino34_magnitude: str
    nino34_until: str
    rain: tuple[RainEntry, ...]

    @property
    def current_magnitude(self) -> EnfenMagnitude:
        return sorted(self.nino12_forecast)[0][1] if self.nino12_forecast else EnfenMagnitude.NEUTRAL


@dataclass(frozen=True)
class EnfenReconciliation:
    number: str
    verified: bool
    flags: tuple[str, ...]
    matched: bool  # the number exists in the curated timeline
    date: date | None
    alert_status: EnfenAlertStatus | None
    magnitude: EnfenMagnitude | None
    rain_by_region: dict[str, RainfallAnomaly]
    extracted_magnitude: EnfenMagnitude
    extracted_date: date
    extracted_status: EnfenAlertStatus


@dataclass(frozen=True)
class EnfenHazardInputs:
    number: str
    date: date
    enfen_magnitude: EnfenMagnitude
    alert_status: EnfenAlertStatus
    seasonal_rainfall_anomaly: RainfallAnomaly | None
    region: str
    flags: tuple[str, ...]


# --- model call ---------------------------------------------------------------------------------


def extract_enfen(pdf: bytes, filename: str, extractor: StructuredExtractionPort, model: str | None = None) -> EnfenExtraction:
    data_url = "data:application/pdf;base64," + base64.b64encode(pdf).decode()
    raw = extractor.extract_json(
        instructions=INSTRUCTIONS,
        content=[
            {"type": "input_text", "text": "Extract the fields of this ENFEN communique."},
            {"type": "input_file", "filename": filename, "file_data": data_url},
        ],
        schema_name="enfen_communique",
        schema=ENFEN_SCHEMA,
        purpose="enfen_extract",
        model=model,
    )
    return parse_extraction(raw)


def parse_extraction(raw: dict) -> EnfenExtraction:
    try:
        number = str(raw["number"]).strip()
        if not number:
            raise InvalidExtraction("empty communique number")
        when = date.fromisoformat(str(raw["date"]).strip())
        status = _STATUS[str(raw["alert_status"])]
        months = []
        for item in raw["nino12_forecast"]:
            month = str(item["month"])
            if not _MONTH.match(month):
                raise InvalidExtraction(f"bad month '{month}'")
            months.append((month, EnfenMagnitude(str(item["magnitude"]))))
        if not months:
            raise InvalidExtraction("no Nino 1+2 forecast")
        nino34 = raw["nino34"]
        rain = tuple(
            RainEntry(
                region=str(r["region"]).strip().lower(),
                period=str(r["period"]),
                outlook=_OUTLOOK[str(r["outlook"])],
                quote=str(r["evidence_quote"]),
            )
            for r in raw["rain_outlook"]
            if str(r["period"]) in _PERIODS
        )
        return EnfenExtraction(number, when, status, tuple(sorted(months)), str(nino34["magnitude"]), str(nino34.get("until_month", "")), rain)
    except InvalidExtraction:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidExtraction(f"extraction does not satisfy the contract: {type(exc).__name__}: {exc}") from exc


# --- deterministic checks ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Lower-case ASCII letters and digits only: robust to accents and to encoding damage in extracted text."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


_ABOVE = [normalize(c) for c in ("sobre lo normal", "por encima de lo normal", "superior a lo normal", "superiores a lo normal")]
_BELOW = [normalize(c) for c in ("inferior", "debajo de lo normal", "déficit", "deficit")]


def outlook_from_quote(quote: str) -> RainfallAnomaly | None:
    """Fixed-rule reading of a Spanish rainfall sentence. None when the quote is silent or mixes directions."""
    text = normalize(quote)
    above = any(c in text for c in _ABOVE)
    below = any(c in text for c in _BELOW)
    if above and below:
        return None
    if above:
        return RainfallAnomaly.ABOVE_NORMAL
    if below:
        return RainfallAnomaly.BELOW_NORMAL
    if "normal" in text:
        return RainfallAnomaly.NORMAL
    return None


def reconcile(extraction: EnfenExtraction, bulletins: Sequence[EnfenBulletin], source_text: str | None) -> EnfenReconciliation:
    flags: list[str] = []
    match = next((b for b in bulletins if b.number == extraction.number), None)
    if match is None:
        flags.append("not_in_timeline")
        return EnfenReconciliation(
            extraction.number, False, tuple(flags), False, None, None, None, {},
            extraction.current_magnitude, extraction.date, extraction.alert_status,
        )
    if extraction.date != match.date:
        flags.append("date_mismatch")
    if extraction.alert_status is not match.alert_status:
        flags.append("status_mismatch")
    if extraction.current_magnitude is not match.magnitude:
        flags.append("magnitude_mismatch")

    rain: dict[str, RainfallAnomaly] = {}
    if source_text is None:
        flags.append("source_text_unavailable")
    else:
        haystack = normalize(source_text)
        chosen: dict[str, tuple[int, RainfallAnomaly]] = {}
        for entry in extraction.rain:
            if normalize(entry.quote) not in haystack or not normalize(entry.quote):
                flags.append(f"rain_quote_not_found:{entry.region}")
                continue
            derived = outlook_from_quote(entry.quote)
            if derived is None:
                flags.append(f"rain_quote_ambiguous:{entry.region}")
                continue
            if derived is not entry.outlook:
                flags.append(f"rain_outlook_label_mismatch:{entry.region}")
            rank = _PERIODS.index(entry.period)  # next_summer (0) is preferred over rest_of_year (1)
            if entry.region not in chosen or rank < chosen[entry.region][0]:
                chosen[entry.region] = (rank, derived)
        rain = {region: value for region, (_, value) in chosen.items()}
    return EnfenReconciliation(
        extraction.number, not flags, tuple(flags), True, match.date, match.alert_status, match.magnitude, rain,
        extraction.current_magnitude, extraction.date, extraction.alert_status,
    )


def hazard_inputs_from(reconciliation: EnfenReconciliation, region: str) -> EnfenHazardInputs | None:
    """Curated ENFEN values (+ a verified rainfall outlook for the region) for the HazardContext, or None."""
    if not reconciliation.matched or reconciliation.magnitude is None or reconciliation.date is None or reconciliation.alert_status is None:
        return None
    return EnfenHazardInputs(
        number=reconciliation.number,
        date=reconciliation.date,
        enfen_magnitude=reconciliation.magnitude,
        alert_status=reconciliation.alert_status,
        seasonal_rainfall_anomaly=reconciliation.rain_by_region.get(region.lower()),
        region=region.lower(),
        flags=reconciliation.flags,
    )

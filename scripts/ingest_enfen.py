"""Ingest an ENFEN communique PDF with Muse Spark structured output, then verify it deterministically.

The model extracts {number, date, alert_status, Nino 1+2 magnitude by month, Nino 3.4, rain outlook by region
with verbatim quotes}. The curated enfen_timeline.csv wins on any mismatch and flags are raised; a communique
missing from the timeline is never used. Public official PDFs only (no personal data).

Reads   <data>/raw/enfen/pdf/<file>.pdf, <data>/raw/enfen/txt/<file>.txt (quote check), <data>/processed/enfen_timeline.csv
Writes  <data>/derived/enfen_extractions/<number>.json   (extraction + reconciliation, always)
        <data>/derived/enfen_hazard.json                  (hazard inputs for the API, when the number is in the timeline)

Run:  .venv/Scripts/python -I scripts/ingest_enfen.py --pdf raw/enfen/pdf/enfen_2026_18.pdf --live
      .venv/Scripts/python -I scripts/ingest_enfen.py --pdf raw/enfen/pdf/enfen_2026_18.pdf --extraction-json saved.json   (offline)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.adapters.geodata.timeseries import load_enfen_csv  # noqa: E402
from willay.application.enfen_ingest import (  # noqa: E402
    extract_enfen,
    hazard_inputs_from,
    parse_extraction,
    reconcile,
)

DEFAULT_REGION = "costa central"  # the Rimac basin: Lima coast (the bulletin does not name "sierra central")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, required=True, help="PDF path, absolute or relative to the data folder")
    parser.add_argument("--live", action="store_true", help="call the model (costs a few cents)")
    parser.add_argument("--extraction-json", type=Path, help="reuse a saved raw extraction instead of calling the model")
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args()

    data = DataDir(resolve_data_dir())
    pdf_path = args.pdf if args.pdf.is_absolute() else data.root / args.pdf
    text_path = data.raw("enfen", "txt", pdf_path.stem + ".txt")
    source_text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.exists() else None
    bulletins = load_enfen_csv(data.processed("enfen_timeline.csv"))

    if args.extraction_json:
        raw = json.loads(args.extraction_json.read_text(encoding="utf-8"))
        extraction = parse_extraction(raw)
    elif args.live:
        from willay.adapters.meta_model_api import load_adapter_from_env

        extraction = extract_enfen(pdf_path.read_bytes(), pdf_path.name, load_adapter_from_env(data.root))
    else:
        sys.exit("pass --live to call the model or --extraction-json to reuse a saved extraction")

    reconciliation = reconcile(extraction, bulletins, source_text)
    out_dir = data.derived("enfen_extractions")
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_pdf": pdf_path.name,
        "extraction": {
            "number": extraction.number,
            "date": extraction.date.isoformat(),
            "alert_status": extraction.alert_status.value,
            "nino12_forecast": [{"month": m, "magnitude": mag.value} for m, mag in extraction.nino12_forecast],
            "nino34": {"magnitude": extraction.nino34_magnitude, "until_month": extraction.nino34_until},
            "rain_outlook": [
                {"region": r.region, "period": r.period, "outlook": r.outlook.value, "evidence_quote": r.quote} for r in extraction.rain
            ],
        },
        "reconciliation": {
            "verified": reconciliation.verified,
            "flags": list(reconciliation.flags),
            "in_timeline": reconciliation.matched,
            "curated_magnitude": reconciliation.magnitude.value if reconciliation.magnitude else None,
            "curated_date": reconciliation.date.isoformat() if reconciliation.date else None,
            "deterministic_rain_by_region": {k: v.value for k, v in reconciliation.rain_by_region.items()},
        },
    }
    (out_dir / f"{extraction.number}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    inputs = hazard_inputs_from(reconciliation, args.region)
    if inputs is not None:
        hazard = {
            "number": inputs.number,
            "date": inputs.date.isoformat(),
            "enfen_magnitude": inputs.enfen_magnitude.value,
            "alert_status": inputs.alert_status.value,
            "region": inputs.region,
            "seasonal_rainfall_anomaly": inputs.seasonal_rainfall_anomaly.value if inputs.seasonal_rainfall_anomaly else None,
            "flags": list(inputs.flags),
            "verified": reconciliation.verified,
            "source": f"ENFEN Comunicado Oficial N°{inputs.number}; extracted by Muse Spark, verified by deterministic checks",
        }
        data.derived("enfen_hazard.json").write_text(json.dumps(hazard, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"number": extraction.number, **record["reconciliation"], "hazard_written": inputs is not None}, indent=2))


if __name__ == "__main__":
    main()

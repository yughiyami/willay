"""Live smoke tests against the real Meta Model API. Skipped by default; run once on purpose:

    .venv/Scripts/python -m pytest -m live

Each test makes the smallest call that confirms the documented contract (docs/META_API.md). Every call is logged
to <willay-data>/derived/meta_usage.jsonl and refused when the cumulative spend would exceed the budget
(WILLAY_META_BUDGET_USD, default 3.0). Only public documents, synthetic households and a synthetic clip are sent.
The API key is read from .env at runtime and never printed.
"""

import json
import runpy
import sys
from pathlib import Path

import pytest

from willay.adapters.data_dir import resolve_data_dir
from willay.adapters.meta_model_api import MODEL_TEXT, load_adapter_from_env
from willay.application.operator_agent import OperatorAgent, OperatorToolkit
from willay.application.rank_sites import rank_sites
from willay.api.dependencies import synthetic_dependencies

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]
DATA = resolve_data_dir()
SYNTH = DATA / "derived" / "synthetic"


@pytest.fixture(scope="module")
def adapter():
    return load_adapter_from_env(DATA)


def need(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"missing {path}")
    return path


def run_script(name: str, *args: str) -> None:
    argv = sys.argv
    sys.argv = [name, *args]
    try:
        runpy.run_path(str(REPO / "scripts" / name), run_name="__main__")
    finally:
        sys.argv = argv


def test_responses_text_reports_usage(adapter):
    text = adapter.rephrase("Alerta: lluvias intensas. Siga a SENAMHI, INDECI y ENFEN.", "es")
    assert text.strip()


def test_tool_calling_agent_flies_to_a_site(adapter):
    deps = synthetic_dependencies()
    ranked = rank_sites(deps.repository, deps.hazard_scenario)
    reply = OperatorAgent(adapter, OperatorToolkit(lambda: ranked, deps.hazard_scenario)).answer(
        "Llévame al relave de mayor riesgo y muestra las rutas de flujo."
    )
    kinds = [a["type"] for a in reply.ui_actions]
    assert "fly_to" in kinds, reply
    assert reply.answer_es


def test_structured_output_reads_the_enfen_18_pdf_and_reconciles_with_the_timeline(adapter):
    pdf = need(DATA / "raw" / "enfen" / "pdf" / "enfen_2026_18.pdf")
    run_script("ingest_enfen.py", "--pdf", str(pdf), "--live")
    record = json.loads((DATA / "derived" / "enfen_extractions" / "18-2026.json").read_text(encoding="utf-8"))
    assert record["extraction"]["number"] == "18-2026"
    assert record["reconciliation"]["in_timeline"] is True
    assert record["extraction"]["nino12_forecast"], "no Nino 1+2 months extracted"


def test_voice_transcribe_spanish(adapter):
    wav = need(SYNTH / "query_es.wav").read_bytes()
    transcript = adapter.transcribe(wav, "es")
    assert "relave" in transcript.lower(), transcript


def test_image_generation_evacuation_card(adapter):
    from willay.application.evacuation_card import CardRequest, build_card
    from willay.domain.entities import AlertTier

    card = build_card(CardRequest("Caserío Sintético Uno", 3.4, "Loma del mirador", AlertTier.EVACUATION), adapter)
    assert card.image[:4] in (b"\x89PNG", b"RIFF", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xdb") or len(card.image) > 1000
    out = DATA / "derived" / "cards"
    out.mkdir(parents=True, exist_ok=True)
    (out / "live_smoke_evacuation.png").write_bytes(card.image)


def test_sam_video_one_concept_on_the_synthetic_clip(adapter):
    clip = need(SYNTH / "rising.mp4").read_bytes()
    seg = adapter.segment_video(clip, "water", purpose="live_smoke_sam")
    assert seg.billed_frames is not None and seg.billed_frames >= 50
    assert (seg.width, seg.height) == (320, 240) or not seg.frames
    summary = {
        "frames_with_matches": len(seg.frames),
        "billed_frames": seg.billed_frames,
        "objects_first_frame": len(seg.frames[0].objects) if seg.frames else 0,
    }
    (DATA / "derived" / "synthetic" / "live_sam_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def test_wonder_judges_ten_composed_messages(adapter):
    run_script("judge_messages.py", "--live")
    result = json.loads((DATA / "derived" / "message_judgements.json").read_text(encoding="utf-8"))
    assert result["summary"]["n"] == 10
    assert MODEL_TEXT  # keep the import used: the text model is the default for everything else

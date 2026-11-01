"""Record the live orchestrator smoke run once, to use as the offline demo fixture.

Runs the whole demo script against the real Meta models (planner muse-wonder-1.0, specialists muse-spark-1.3, SAM 3.1 on the
FAKE synthetic clip, Muse Image on a synthetic evacuation card) with the simulated operator click, and writes:

  backend/fixtures/demo/orchestrator_recording.json   every model turn, the SAM output and the card (replayed by Demo mode)
  backend/fixtures/demo/live_trace.json                the full trace of that live run (agents, models, tools, latency, cost)

Costs real credits (free-credit content may be used by Meta, so only synthetic data is sent: no people, no real households).
The spend of THIS run is capped by --budget on top of what the usage log already holds; every call is logged to
<data>/derived/meta_usage.jsonl.

Run:  .venv/Scripts/python -I scripts/record_demo.py --live --yes-synthetic-only [--budget 1.0]
"""

from __future__ import annotations

import argparse
import base64
import faulthandler
import io
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

OUT_DIR = ROOT / "backend" / "fixtures" / "demo"
CARD_WIDTH = 640


def downscale_png(data: bytes, width: int = CARD_WIDTH) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="call the real Meta models (costs credits)")
    parser.add_argument("--yes-synthetic-only", action="store_true", help="confirm that only synthetic data is sent (no people, no real households)")
    parser.add_argument("--budget", type=float, default=1.0, help="most this run may spend, in USD")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()
    if not (args.live and args.yes_synthetic_only):
        sys.exit("this script spends credits: pass --live and --yes-synthetic-only")

    os.environ["WILLAY_META_ENABLED"] = "1"
    faulthandler.dump_traceback_later(args.timeout + 60, exit=True)  # a hang dumps every thread's stack and exits
    from fastapi.testclient import TestClient

    from willay.adapters.meta_model_api import load_adapter_from_env
    from willay.adapters.replay_models import RecordingAgentModel, RecordingSegmenter
    from willay.api.app import create_app
    from willay.api.dependencies import orchestrator_config_from_env, real_dependencies
    from willay.application.orchestration.demo_script import pick_demo_site
    from willay.application.rank_sites import rank_sites

    adapter = load_adapter_from_env()
    usage = adapter.usage_log
    assert usage is not None
    start_total = usage.total_cost()
    usage.budget_usd = start_total + args.budget  # the guard refuses any call past this line
    print(f"usage log holds ${start_total:.4f}; this run may spend up to ${args.budget:.2f}")

    deps = real_dependencies()
    ranked = rank_sites(
        deps.repository, deps.hazard_scenario, exposure_config=deps.exposure_config,
        intakes=list(deps.repository.list_water_intakes()), basin_only=True,
    )
    site = pick_demo_site(ranked, deps.households)
    if site is None:
        sys.exit("no site with demo households downstream")

    recorder = RecordingAgentModel(adapter, site.site.id)
    segmenter = RecordingSegmenter(adapter)
    deps.agent_model = recorder
    deps.video_segmenter = segmenter
    deps.image_generator = adapter
    deps.usage_log = usage
    deps.orchestrator_config = orchestrator_config_from_env(os.environ)
    deps.demo_pace_s = 0.0
    client = TestClient(create_app(deps))

    started = time.time()
    run = client.post("/demo/run", json={"mode": "live", "auto_confirm": True}).json()
    run_id = run["run_id"]
    print(f"demo run {run_id} on {site.site.name} ({site.site.id})")
    seen = 0
    while time.time() - started < args.timeout:
        trace = client.get(f"/agent/trace/{run_id}").json()
        for event in trace["events"][seen:]:
            label = event["tool"] or event["model"] or event["result_summary"][:60]
            print(f"  {event['seq']:3d} {event['kind']:15s} {event['agent']:15s} {label[:60]}  {event['result_summary'][:70] if event['kind'] != 'step' else ''}")
        seen = len(trace["events"])
        if trace["status"] != "running":
            break
        time.sleep(1.0)
    else:
        print("timed out waiting for the demo", flush=True)
        os._exit(1)

    spent = usage.total_cost() - start_total
    print(f"\nstatus: {trace['status']}  trace cost ${trace['totals']['cost_usd']:.4f}  usage-log delta ${spent:.4f}")
    print("models:", trace["totals"]["models"])
    if trace["status"] != "completed":
        sys.exit(f"the live run did not complete: {trace['result']}")

    artifacts = deps.traces.get(run_id).artifacts  # type: ignore[union-attr]
    card = artifacts.get("card.png")
    recording = {
        "recorded_with": {
            "date": date.today().isoformat(),
            "data_mode": "real",
            "site_id": site.site.id,
            "site_name": site.site.name,
            "planner_model": deps.orchestrator_config.planner_model,
            "specialist_model": deps.orchestrator_config.specialist_model,
            "cost_usd": round(spent, 4),
            "note": "synthetic clip and synthetic households only; the card is a Muse Image output and needs human review",
        },
        "calls": recorder.to_dict()["calls"],
        "segmentations": segmenter.to_dict(),
        "card_png_base64": base64.b64encode(downscale_png(card[1])).decode() if card else None,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "orchestrator_recording.json").write_text(json.dumps(recording, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT_DIR / "live_trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT_DIR / 'orchestrator_recording.json'} and live_trace.json", flush=True)
    os._exit(0)  # the test client keeps worker threads alive; everything is written


if __name__ == "__main__":
    main()

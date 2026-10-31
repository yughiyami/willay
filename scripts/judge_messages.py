"""Compose 10 Spanish household alert messages from the real ranking and have Muse Wonder judge them (advisory).

Messages use only public data (INGEMMET site names, computed distances, the ENFEN communique citation): no personal
data. The tier of each message is a scenario input cycling through the household tiers; the judge never sets it.
One batched request (about 6k output tokens at most, a few cents) is logged to <data>/derived/meta_usage.jsonl.

Writes <data>/derived/message_judgements.json

Run:  .venv/Scripts/python -I scripts/judge_messages.py --dry-run      (prints the messages, no API call)
      .venv/Scripts/python -I scripts/judge_messages.py --live
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from willay.adapters.data_dir import DataDir, resolve_data_dir  # noqa: E402
from willay.api.dependencies import real_dependencies  # noqa: E402
from willay.application.compose_alert_message import AlertMessageInput, action_text, compose_alert_message  # noqa: E402
from willay.application.judge_messages import JudgeItem, judge_messages  # noqa: E402
from willay.application.rank_sites import rank_sites  # noqa: E402
from willay.domain.entities import AlertTier  # noqa: E402

TIERS = (AlertTier.PREPARATION, AlertTier.ATTENTION, AlertTier.EVACUATION)


def build_items(count: int = 10) -> list[JudgeItem]:
    deps = real_dependencies(DataDir(resolve_data_dir()))
    ranked = rank_sites(
        deps.repository, deps.hazard_scenario, exposure_config=deps.exposure_config,
        intakes=list(deps.repository.list_water_intakes()), basin_only=True,
    )
    items: list[JudgeItem] = []
    for ranked_site in ranked:
        nearest = ranked_site.exposure.nearest if ranked_site.exposure else None
        if nearest is None:
            continue
        tier = TIERS[len(items) % len(TIERS)]
        data = AlertMessageInput(
            tier=tier,
            site_name=ranked_site.site.name,
            distance_km=nearest.along_path_m / 1000.0,
            safe_point=None,
            official_source=deps.official_source,
        )
        message = compose_alert_message(data, llm=None, language="es")
        items.append(
            JudgeItem(
                f"m{len(items) + 1}", tier, message.text, data.site_name, data.distance_km, data.safe_point,
                data.official_source, approved_text=action_text(tier, "es"),
            )
        )
        if len(items) == count:
            break
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    items = build_items(args.count)
    if not args.live:
        for item in items:
            print(item.id, item.text)
        return
    from willay.adapters.meta_model_api import load_adapter_from_env

    data = DataDir(resolve_data_dir())
    report = judge_messages(items, load_adapter_from_env(data.root))
    result = report.to_dict()
    result["messages"] = [{"id": i.id, "tier": i.tier.value, "text": i.text} for i in items]
    out = data.derived("message_judgements.json")
    previous = out.with_name("message_judgements_run1_facts_only_prompt.json")
    if out.exists() and not previous.exists():
        out.rename(previous)  # keep the first run (judge saw only the facts, so it called the fixed action text "additions")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

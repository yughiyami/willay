"""Measure the water rise on a marker in a clip (max 15 s, no people in frame).

Default segmenter is the offline colour-threshold stand-in, which only works on the synthetic clip.
`--live` calls sam-3.1 (two requests: water + marker; billed per frame, 50-frame minimum each) and is
logged to <data>/derived/meta_usage.jsonl with the spend budget enforced. Only record clips WITHOUT people:
free-credit content may be used by Meta for product improvement.

Run:  .venv/Scripts/python -I scripts/watch_clip_cli.py --clip clip.mp4 --marker-cm 100 [--live]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from willay.adapters.clip_media import ColorSegmenter, probe_clip  # noqa: E402
from willay.application.watch_clip import DEFAULT_MARKER_PROMPT, DEFAULT_WATER_PROMPT, measure_clip  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clip", type=Path, required=True)
    parser.add_argument("--marker-cm", type=float, required=True, help="visible marker length at the start of the clip, in cm")
    parser.add_argument("--water-prompt", default=DEFAULT_WATER_PROMPT)
    parser.add_argument("--marker-prompt", default=DEFAULT_MARKER_PROMPT)
    parser.add_argument("--live", action="store_true", help="use sam-3.1 (costs money)")
    parser.add_argument("--yes-no-people", action="store_true", help="confirm the clip contains no people (required with --live)")
    args = parser.parse_args()

    video = args.clip.read_bytes()
    info = probe_clip(video, args.clip.suffix or ".mp4")
    if info.duration_s > 15.25:
        sys.exit(f"clip is {info.duration_s:.1f} s; the limit is 15 s")
    if args.live:
        if not args.yes_no_people:
            sys.exit("--live sends the clip to a third-party model: pass --yes-no-people to confirm it shows no people")
        from willay.adapters.meta_model_api import load_adapter_from_env

        segmenter = load_adapter_from_env()
    else:
        segmenter = ColorSegmenter()
    result = measure_clip(
        video, segmenter, args.marker_cm, water_prompt=args.water_prompt, marker_prompt=args.marker_prompt, total_frames=info.frames
    )
    watch = result.watch
    print(
        json.dumps(
            {
                "rise_cm": None if watch.rise_cm is None else round(watch.rise_cm, 1),
                "confidence": watch.confidence,
                "valid_frames": watch.valid_frames,
                "total_frames": watch.total_frames,
                "reasons": list(watch.reasons),
                "billed_frames": result.billed_frames,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

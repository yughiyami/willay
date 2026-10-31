"""Generate a FAKE rising-water clip (red post in a blue river) for an end-to-end dry run.

The scene is synthetic: no real river, no people. It writes an MP4 plus a `<name>.truth.json` with the exact
ground truth (marker height, rise in cm, water-line row per frame), so the watcher pipeline can be checked
offline and for free. The default clip has 60 frames (SAM 3.1 bills at least 50 frames per request).

Run:  .venv/Scripts/python -I scripts/make_synthetic_clip.py --out ../willay-data/derived/synthetic/rising.mp4
      .venv/Scripts/python -I scripts/watch_clip_cli.py --clip ../willay-data/derived/synthetic/rising.mp4 --marker-cm 100
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from willay.adapters.synthetic_clip import SyntheticClipSpec, render_clip, write_mp4  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="output .mp4 path")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--marker-cm", type=float, default=100.0, help="visible marker length at the start of the clip")
    parser.add_argument("--rise-cm", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    spec = SyntheticClipSpec(seconds=args.seconds, fps=args.fps, marker_height_cm=args.marker_cm, rise_cm=args.rise_cm, seed=args.seed)
    frames, truth = render_clip(spec)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_mp4(frames, spec.fps, args.out)
    truth_path = args.out.with_suffix(".truth.json")
    truth_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"wrote {args.out} ({truth['frames']} frames, {args.out.stat().st_size} bytes) and {truth_path.name}")


if __name__ == "__main__":
    main()

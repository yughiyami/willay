# River watcher: field protocol and validation plan

The watcher turns a short clip of a painted marker standing in a river into a water-rise measurement in centimeters.
It is a **measurement for the operator**, never an alert: tiers come only from the domain rules. A rise value can be passed
to `/alerts/evaluate` as `water_rise_cm`, and EVACUATION still needs a human to confirm.

## How it measures (deterministic geometry on SAM masks)

1. Two `sam-3.1` requests over the clip (one noun phrase each): the water (default prompt `water`) and the marker
   (default `red post`; change it with `marker_prompt`, for example `measuring stick`).
2. The marker's pixel length in the first frames (the part above the water) is matched to the **known marker height** in cm:
   `cm_per_px = marker_height_cm / marker_px`.
3. Per frame, the water line is the topmost row, from the marker top down, where the water mask covers at least half of the
   marker columns (with a one-marker-width margin on both sides) for three consecutive rows. Isolated noise does not count.
4. `rise_cm` is the displacement of the water line from its starting row (median of the last 10% of valid frames).
   `confidence` = valid-frame fraction x line smoothness x marker stability x mean SAM confidence. Reasons such as
   `marker_not_found`, `too_few_valid_frames` or `marker_moves_between_frames` explain a missing or weak result.
5. Output: `rise_cm`, `confidence`, per-frame series, three overlay frames (water blue, marker red, water line green) and the
   billed frames and cost. Code: `domain/water_line.py` (pure, tested on synthetic masks), `application/watch_clip.py`,
   `POST /watch/clip`, CLI `scripts/watch_clip_cli.py`.

## Recording protocol (what a clip must look like)

- **No people in frame.** Clips go to a third-party model and free-credit content may be used by Meta. The API refuses a
  clip unless `no_people_confirmed=true`.
- MP4 or MOV, H.264/H.265, **at most 15 s**, camera fixed on a tripod or a rigid mount (a moving camera breaks the scale).
  Landscape or portrait, but keep the video's rotation flag consistent.
- One straight post, vertical, planted firmly, **painted a single saturated colour** (red or orange) with high contrast to the
  background, at least 1 m of it visible. Measure its length with a tape and write it down: that is `marker_height_cm`.
  Define it as **the length visible above the water at the start of the clip** (start recording when you have read the water
  at a graduation or at the post's base).
- Water line roughly horizontal across the post, no strong reflections of the post, no debris wrapped around it. Avoid
  backlight and rain on the lens. Frame so the post is at least 15% of the image height.
- Record 8-15 s; for rising water start the clip while the level is still near the starting mark.

## Validation plan (to be done with the user's own recordings)

The synthetic clip only proves the pipeline. Accuracy on real water is **unmeasured**. Plan:

1. Record **30-40 clips** at different sites/times, light (clear, overcast, low sun), flow speeds, camera distances and water
   colours. Include at least 5 clips with no change (stable water) and 5 with a visible rise.
2. For each clip write a row in `clips.csv`: `clip_file, marker_height_cm, water_start_cm, water_end_cm, notes`, where the
   water readings come from a graduated tape or staff gauge read by a person at the start and end of the clip. Ground truth
   rise = `water_end_cm - water_start_cm`.
3. Run `scripts/watch_clip_cli.py --clip <file> --marker-cm <height> --live --yes-no-people` and collect `rise_cm`. Cost:
   two SAM requests, each billed per frame ($0.20 per 1,000 frames, 50-frame minimum): about $0.07 for a 6 s clip at 30 fps,
   $0.18 for 15 s at 30 fps. Water moves slowly, so re-encoding to 10 fps (`ffmpeg -i in.mp4 -r 10 -c:v libx264 out.mp4`)
   cuts that to about $0.02-0.06 (not yet checked for accuracy: include a few clips at both rates).
4. Report **MAE of `rise_cm` against ground truth, target <= 10 cm**, plus the median error, the worst clip, the share of clips
   with `rise_cm = null`, and the calibration of `confidence` (error should fall as confidence rises). Split the results by
   light and by camera distance. Keep a held-out third of the clips: tune prompts and thresholds only on the rest.
5. A clip that returns no measurement counts as a failure in the MAE denominator report (show both: MAE on measured clips
   and the coverage), never silently dropped.
6. Decide whether the watcher is fit for the EVACUATION rule only if MAE <= 10 cm with acceptable coverage; otherwise it stays
   an operator aid. A community-watcher report (`watcher_report`) remains an alternative trigger in the rules.

Budget: 40 clips cost about $2-3 at 10 fps or up to about $7 at 30 fps and 15 s each.

## Dry run on the synthetic clip

```bash
.venv/Scripts/python -I scripts/make_synthetic_clip.py --out ../willay-data/derived/synthetic/rising.mp4   # 60 frames
.venv/Scripts/python -I scripts/watch_clip_cli.py --clip ../willay-data/derived/synthetic/rising.mp4 --marker-cm 100
```

The default segmenter is an offline colour-threshold stand-in (it only works on this fake scene): it returned a 30.0 cm rise
for a 30 cm ground truth (confidence 0.9). One live SAM request ("water" only) was made on the same clip to confirm the
contract; see `docs/META_API.md`.

## Known limits

- The scale assumes the post is roughly perpendicular to the camera axis and the lens distortion is small at the post.
- A very thin post, a post that leaves the frame, or water covering the post's top gives `marker_not_found` or a wrong scale.
- The geometry reads the water mask next to the post; foam, reflections and wet banks can confuse the mask. The overlay frames
  exist so a human can check what was measured.
- Falling water is reported as a negative rise.

## Prompt wording (measured once, on the fake clip)

On the synthetic scene (blue river, red post, true rise 45 cm) the water prompt changed the result: `water` 29.2 cm with 35/60 valid
frames, `river` nothing, `blue water` 45.0 cm with 60/60 valid frames and confidence 0.78. The demo uses `blue water`
(`DEMO_WATER_PROMPT`); the watcher tool takes the prompts from the orchestrator services. This says nothing about real rivers: test the
wording on your own clips before relying on it.

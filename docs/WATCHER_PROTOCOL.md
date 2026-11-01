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

The synthetic clip only proves the pipeline. Accuracy on real *river video* is **unmeasured** (a still-image check on a public lake dataset is in "Validation on a public dataset" below). Plan:

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

## Validation on a public dataset (staff gauge images, held out)

Run on 2026-10-31 on the Fang and Duan dataset (Zenodo 10812124, CC BY 4.0; citation and checksum in
`willay-data/raw/zenodo_10812124/SOURCE.md`): 2,691 JPEGs (3840x2160) from **one fixed time-lapse camera on one urban lake in
South China**, 27 May to 6 Jul 2023, with a manual stage reading per image (stage 127.8 to 184.9 cm, SD 5.1 cm; day, night and
heavy rain included; no person was visible in the roughly 20 images inspected, but the full set was not screened). It is a still-image test of the same idea as the
watcher (SAM masks, then geometry, then a calibrated map to centimeters), **not** a test of the video clip pipeline or of a
river. Code: `willay-data/scripts/gauge_*.py`; results: `willay-data/derived/sam_gauge_validation/` (`results.json`,
`per_image.csv`, masks cache, overlays). Spend: US$1.83 of the US$2.00 allowed (about 730 SAM image calls at $2.50 per 1,000, all
logged in `meta_usage.jsonl`); the new adapter method is `MetaModelApiAdapter.segment_image`.

### Method (declared before the evaluation images were seen)

- **Sample**: 250 images, 25 from each of 10 stage bins (so the tails are over-represented), seeded, tuning images excluded.
  **Split**: 20% of each bin (50 images) fit the map, **200 images are held out**. Nothing was tuned on them.
- **Tuning set**: 5 separate images (day, night, dusk, lowest and highest stage decile). On them: `staff gauge` returned
  nothing at all; `measuring ruler` found the gauge in 3 of 5 (the full 3840 px frame found 1 of 5, so images are sent at 1920 px);
  `water` found water in every image but, as the overlays show, it **segments the wet concrete wall beside the gauge** rather
  than the lake.
- **Estimators** (pixel row in the original frame to stage with an ordinary least squares line fitted on the calibration split):
  1. As originally specified: topmost row where the `water` mask covers the gauge's own columns (below the gauge top). Because of the wall problem
     this is a failure (below).
  2. Chosen after tuning: the row where the visible gauge mask ends (the graduated staff disappears under the surface).
     **v1** uses the full 1920 px frame; **v2** (added after v1 showed 27% coverage) crops a fixed 1000x1560 px region around the
     gauge at native resolution (box taken from calibration detections, no labels; adopted because it found the gauge in 5 of 5
     tuning images) and is run only where v1 found no gauge. The combined pipeline uses v1 when it fires, v2 otherwise.
- **Baseline**: predict the calibration-mean stage for every image.

### Numbers on the 200 held-out images

| | Coverage | MAE (cm) | RMSE (cm) | Bias (cm) | within 10 cm |
|---|---|---|---|---|---|
| **Combined pipeline (v1, else v2 ROI)** | 97.5% (195/200) | **4.6** | 6.6 | -0.3 | **83%** (64% within 5 cm) |
| Baseline: calibration mean | 100% | 7.8 | 10.3 | +0.2 | 68% |
| v1 alone (full frame) | **28%** (56/200) | 0.5 | 0.7 | -0.2 | 100% |
| v1 baseline on the same 56 images | | 9.2 | 12.3 | +8.6 | 66% |
| v2 alone (ROI, 144 images v1 missed) | 96.5% | 6.2 | 7.8 | -0.4 | 76% |
| Water mask on gauge columns (as first specified, 55 images) | 27.5% | 14.7 | 24.1 | +11.3 | 62% (baseline on same images: MAE 8.9) |

- Day and night are equally good for the combined pipeline (MAE 4.6 and 4.6 cm; 128 of 130 day and 67 of 70 night images measured).
  Failures to measure: 5 images with no gauge mask in v1 or the ROI (four at night, between 21:00 and 06:00, one at 17:00 in rain at 172.8 cm; see `failure_no_gauge_found.png`).
- **By stage bin (combined; mean true stage of the bin, MAE vs the baseline in the same bin, cm)**: 138: 3.5 vs 19.7; 148: 6.7 vs 10.0;
  152.5: 3.8 vs 5.0; 154: 2.7 vs 3.7; 156: 3.4 vs **1.4**; 158: 2.6 vs **0.4**; 160: 1.0 vs 2.8; 163: 1.9 vs 5.1; 169: **8.9** vs 11.4; 176:
  **11.4** vs 18.1.
- **Re-weighted to the natural stage distribution** (most days sit at 155 to 162 cm) the combined MAE is 2.9 cm and the baseline's
  is also 2.9 cm: on ordinary days predicting the mean is as good as the method. The stratified sample is what makes the 10 cm
  target hard, and the method earns its keep only in the tails.
- For context only, the authors' own method outputs stored in the dataset workbook give MAE 0.95 cm (adaptive SAM, fine-tuned),
  2.55 cm (plain SAM), 8.4 cm (pixel intensity) and 8.0 cm (Canny) on the same 200 images; they are not comparable to a map fitted on 50
  images with a zero-shot prompt.
- v1 alone is accurate (0.5 cm, 0.105 cm per pixel) but it fired on only 27% of the images, mostly at low stages and on two
  days, and **never above 166 cm**, so its figure describes a selected subset. in some images v2's mask extends over the gauge's reflection or
  its submerged continuation (largest error 20 cm, `failure_largest_error.png`), which is why it is noisier
  (0.024 cm per pixel, a weak map).

### What this does and does not show

- SAM 3.1 can find a painted staff gauge zero-shot from a text prompt, and the row where it ends tracks the surface within a few
  centimeters at one site. The prompt matters a lot (`staff gauge` returns nothing, `measuring ruler` works, `water` labels the wall).
- At the stage range that matters for a flood (the top bins) the error is 9 to 11 cm, at the edge of the 10 cm target; in the middle of the
  range the method does not beat a constant. **It does not clear the bar to drive EVACUATION**: it stays an operator aid, as in
  the protocol above.
- **Caveats**: one site in South China; a lake, not a river (no current, debris, turbidity swings or banks like the Rimac); one fixed
  camera whose pixel scale is identical across the 40 days (a moved or re-zoomed camera needs a new map); each site needs its own
  calibration and a visible, painted gauge; the ROI box was found by looking at the same camera; 50 calibration images
  (only 11 for v1); the labels have 0.1 cm resolution but the manual reading itself has an unknown error; a stratified sample, so
  the table is not an operating-condition forecast; the variants v1 to v2 were added after seeing the evaluation images'
  coverage (but not their errors, and v1 is reported alone).
- Overlays (water blue, gauge red, end of the gauge row marked yellow, downscaled derivatives of CC BY 4.0 images by Fang and Duan
  2024) are in `docs/media/gauge_validation/`: `good_day`, `good_night`, `high_stage`, `low_stage`, and the failures
  `failure_largest_error` and `failure_no_gauge_found`. The Evidencia tab shows the summary line (`GET /validation/gauge`).

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

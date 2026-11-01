# Vigía demo asset: a real river timelapse (optional)

- `mendenhall_timelapse_640.mp4`: 100-frame timelapse (10 s at 10 fps, 22-31 Oct 2026), re-encoded by us to 640x362 H.264 without audio from
  the original `timelapse_720.mp4` that the USGS National Imagery Management System serves for camera
  `AK_Mendenhall_River_at_Melvin_Park_near_Auke_Bay` (Mendenhall River at Melvin Park near Auke Bay, Alaska).
- `overlay_000/025/050/075/099.png`: frames 0, 25, 50, 75 and 99 with the SAM 3.1 water mask in blue (prompt "water"). Frame 50 is
  fog: no mask was returned for it, and that is shown on purpose.
- `sam_live_result.json`: the run summary and per-frame mask statistics (one live run, 200 billed frames, about US$0.04).

**Licence:** public domain. USGS-authored or produced data and information are in the U.S. Public Domain
(https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits; the same page warns that some items on USGS sites are
third-party, and this was not verified image by image). No attribution is required; we credit it anyway:
"Image courtesy of the U.S. Geological Survey."

**Clip check:** no people in any frame (34 of 100 frames inspected by eye, every third frame), no audio track.

**What this is NOT:** a validation. No staff gauge is in view, the camera view drifts, 22 of 100 frames are fog, and the river is tidal. The
reference stage (USGS site 15052850, about 1.6 miles downstream, provisional data) is kept in `willay-data/raw/water_clips/usgs_nims/`;
the water-mask area correlated only weakly with it (Spearman 0.26 on the 78 clear frames), so the mask area is not a stage measure and the
watcher produced no `rise_cm` for this clip (`marker_not_found`).

Full provenance, hashes and the other screened clips: `willay-data/raw/water_clips/SOURCES.md`.

# insta-roller

Turns Insta360 X5 360° recordings into finished videos automatically. The full
design is in `Insta360 X5 – automatyczne kadrowanie i montaż (moduł 1).md`.

**Status: stage 1** – one flight, one "flight summary" reel. No Media SDK, no UI,
no face recognition.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ffmpeg]"
```

FFmpeg is taken from `INSTA_ROLLER_FFMPEG`, then `PATH`, then the bundled
`imageio-ffmpeg` binary.

## Use

A job is a folder holding the recording and the IGC log. Originals are never
modified; everything is written to `<folder>/insta-roller` or to `--out`.

```powershell
insta-roller run "C:\flights\2026-06-24" --out work\flight --offset 14
insta-roller run "C:\flights\2026-06-24" --out work\flight --offset 14 --length 25
```

The second call makes a new version with a different length. It reuses the
analysis and every shot that did not change.

Each step is also its own command, and each is idempotent:

| Command | Result |
| --- | --- |
| `analyze` | `scenes.json`, `scenes_report.txt` |
| `plan` | `reel_plan_<theme>_<length>s.json`, `framing_plan_<theme>_<length>s.json` |
| `render` | `reel_<theme>_<length>s.mp4` |
| `preview --at <seconds>` | one still per preset, to check the mount calibration |
| `phases <file.igc>` | flight phases of a log, to compare with your own notes |
| `schemas <folder>` | JSON schemas of the data contracts |

The framing plan can be edited by hand before `render`; it is only regenerated
when the reel plan changes or with `--force`.

### Video source

- **Insta360 Studio export** (`.mp4`, equirectangular, stabilisation off) – the
  intended stage 1 input.
- **LRV proxy** (`LRV_*.lrv` next to the `.insv` files) – a rough dual-fisheye
  stitch done by FFmpeg. Low resolution and a visible seam; good enough to test
  scene and framing logic without any manual export.

A take that the camera split into several files is joined into one timeline.
With several takes or several IGC files in the folder, the pair that overlaps
most is used; `--recording` and `--igc` override that.

### Sync

Video time zero is the camera clock (`creation_time`) plus `--offset` seconds.
`--video-start` sets the UTC start time directly. Automatic sync comes in stage 3.

## Configuration

Nothing is hard-coded: `profiles/flight.yaml` (detection thresholds, scene
weights, shot lengths, framing per scene type), `profiles/mounts/*.yaml`
(calibration and preset angles), `profiles/themes/*.yaml` (which scenes a reel uses).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests build a synthetic flight with known phase times and run the whole
pipeline on a generated clip. A sample run on a real flight is in
`docs/sample-run.md`.

# insta-roller

Turns Insta360 X5 360° recordings into finished videos automatically. The full
design is in `Insta360 X5 – automatyczne kadrowanie i montaż (moduł 1).md`.

| Stage | What | State |
| --- | --- | --- |
| 1 | One flight, one reel | done, run on real flights |
| 2 | Themes, several reels, separate shots, safe zone, horizon, exposure | done, run on real flights |
| 3 | Camera GPS / gyro, automatic sync, flights without IGC, motor glider | done; motor glider untested (no footage) |
| – | Flight data overlay (speed, altitude, vario) | done |
| 4 | Media SDK stitcher (`stitcher/`) | done: built with VS 2022 Build Tools, used for rendering by default; slow (about 15 s per second of 5.7K footage) |
| 5 | Holiday profile | done from sensors and sound; see limits below |
| 6 | People from Immich (`core/faces.py`) | done: exported from a real Immich 3.4.2 and recognised on real clips; people are sampled three times per scene, not tracked frame by frame |
| 7 | Standalone Windows app with its own window: queue, job runner, UI (`app/`) | done; real jobs run end to end on this PC; network-drive and UNC job folders, and the folder dialog by hand, untried |

`docs/sample-run.md` records runs on real footage.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ffmpeg]"
```

FFmpeg is taken from `INSTA_ROLLER_FFMPEG`, then `PATH`, then the bundled
`imageio-ffmpeg` binary. NVENC is used when the GPU offers it.

## Use

A job is a folder with the camera files (`VID_*.insv`, `LRV_*.lrv`) and, for a
flight, the IGC log. Originals are never modified; everything is written to
`<folder>/insta-roller` or to `--out`, caches to `--work`.

```powershell
insta-roller run "D:\flights\2026-06-23" --out work\flight
insta-roller run "D:\flights\2026-06-23" --out work\flight --length 25 --themes flight_summary
insta-roller run "D:\flights\2026-06-23" --out work\flight --enable landing --shots
insta-roller run "D:\flights\2026-06-23" --out work\flight --lengths thermal=45,surprise=60
insta-roller run "D:\holiday\cruise" --out work\cruise --mode both
```

The profile is guessed: an IGC file in the folder means `flight`, otherwise
`holiday`. `--profile flight_motor` selects the motor glider.

Each step is its own idempotent command; `run` calls them in order. A new
length or theme reuses the analysis and every shot that did not change.

| Command | Result |
| --- | --- |
| `analyze` | `scenes.json`, `telemetry.json`, `scenes_report.txt` |
| `plan` | `reels.json`, and per reel `reel_plan_<tag>.json`, `framing_plan_<tag>.json` |
| `render` | `reel_<tag>.mp4` for every planned reel |
| `shots` | `shots_<tag>/NN_<type>.mp4`: the shots as separate H.264 files with spare footage at both ends |
| `preview --at <s>` | one still per preset with the safe zone shaded |
| `proxy` | low-resolution working copies of takes without an LRV file |
| `phases <file.igc>` | flight phases of a log |
| `profiles` | available profiles, mounts and themes |
| `schemas <folder>` | JSON schemas of the data contracts |
| `faces-export`, `faces-check` | people from Immich (stage 6) |

`<tag>` is `<theme>_<length>s`, e.g. `thermal_30s`.

Manual edits survive: a reel plan with `"edited": true` is kept by `plan`, and
a framing plan is only regenerated when its reel plan changes or with `--force`.
`run --review` stops after `plan` so the plans can be edited before `render`.

## Flight profile

### Reels and themes

A theme (`profiles/themes/*.yaml`) is a template: which scene types a reel
uses, how they are framed, its length and how much material it needs. The
profile lists the themes; one reel is made per theme with enough material.

- `takeoff_tow`, `thermal`, `glide_views`: a scene goes to one reel only.
- `flight_summary` borrows scenes from the others.
- `surprise` ("Zaskocz mnie") also borrows, and shows the flight in views
  the other reels never use: a small planet, round ultra-wide views, a slow
  look all the way round, the launch as a time-lapse. Its `variants` give the
  shots of one scene type different framings in turn; `speed` sets a
  playback speed per scene type.
- `landing` is switched off in its file. While it is off no reel uses circuit
  or touchdown scenes and the summary ends on the pilot. `--enable landing`
  turns it on for a job; a recorded landing then ends the summary too.
- A new theme is one new YAML file plus its name in the profile or `--themes`.

`reels.json` says which reels were planned and why any was left out.

### Data sources and sync

In order of preference:

1. **IGC log.** The video clock is tied to it automatically: turn rate from
   the camera's gyroscope is correlated with the turn rate of the track. On
   the two test flights this gives correlation 0.8–0.9 and the same answer for
   every 30-minute file to within about half a second.
2. **GPS remote data stored in the camera file** (`--no-igc`, or no IGC in the
   folder). Scenes come from the camera's own 10 Hz GPS track.
3. **Gyroscope only.** Thermals with direction and glides; no takeoff or landing.

`--video-start` turns automatic sync off; `--offset` shifts its result.
The camera clock itself was 10–14 s behind GPS time on the test flights, and
the phone's IGC is about 2 s ahead of the GPS remote's time stamps.

A recording that ends before the landing is detected by comparing it with the
track; the missing phases are listed in `scenes.json` and the reels end on the
pilot instead.

### Video source

Analysis runs on sensor data only. Rendering uses, in this order:

- the Media SDK stitcher when it is built (`--source sdk` insists on it),
- the full-quality `VID_*.insv` files, stitched roughly by FFmpeg,
- the `LRV_*.lrv` proxy (`--source proxy`: fast, soft, for trying things out),
- an equirectangular `.mp4` exported from Insta360 Studio.

The FFmpeg stitch (`--source ffmpeg`) has a visible seam and no stabilisation;
it is the quick fallback. `--source proxy` is quicker still.

### Framing

Mounts (`profiles/mounts/*.yaml`) hold the calibration and the presets.
Per preset: angles, field of view, `horizon` (`aircraft` banks with the glider,
`locked` keeps the horizon level using the bank angle from the gyroscope and
speed), `exposure_ev` and `projection` (`flat`, `planet` = stereographic
small planet, `fisheye`; one projection per shot).

`safe_zone` in the profile describes what the TikTok / Instagram interface
covers. Framing moves each preset's subject to the centre of the free area and
the overlay stays inside it; `preview` shades the covered parts.

`motorglider_canopy` has the presets `instruments`, `left_seat`, `right_seat`
and `crew`; its `aliases` decide whether "pilot" shots show one seat or both.
Its angles are first guesses and need a look at real footage.

Camera moves inside a framing: `pan_dps` (slow turn), `fov_end` (slow zoom
in or out while the view is held), `roll_turns` (the picture rolls over).
Two presets with the `planet` projection in one shot unroll the small planet
into a normal view. Themes list several framings per scene type in `variants`,
so the shots of a reel are not all framed alike.

### AI framing

`--ai-framing local|openrouter` (or `ai_framing.engine` in the profile, or the
choice in the app's wizard) lets a model pick the framing of every shot. The
candidates are the theme's own framings plus the ordinary ones other themes
use for the same scene type; each is rendered as a small still from the middle
of the shot.

- `local`: CLIP ViT-B/32 (ONNX, about 610 MB, downloaded once to
  `%LOCALAPPDATA%\insta-roller\models`) scores the stills against the
  sentences in `ai_framing.good` / `bad`; a framing the reel already used
  loses `variety`. Needs `pip install -e ".[ai]"`. Nothing leaves the PC.
- `openrouter`: a vision model (`ai_framing.model`, default
  `anthropic/claude-sonnet-5.5`) directs the reel. It is told the footage is a
  full 360 sphere and gets, for every shot, the whole sphere as a panorama
  with a grid of angles plus the ready-made views. It aims the camera itself:
  any yaw, pitch and field of view, a slow zoom or pan, a projection, one to
  three views in a row, with the rhythm of the whole film in mind. In a second
  round it sees what its own views really show and aims the poor ones again.
  Shots of the pilot / holder (`ai_framing.person_shots`, default 2) and
  showpieces such as the small planet (`ai_framing.accents`, default 2) are
  rationed; numbers are clamped and paths that are not valid are ignored.
  The stills and the shot list (type, length, view names) are sent to
  OpenRouter; no GPS, IGC or file names. The key is read from
  `OPENROUTER_API_KEY` in the environment or `.env`.

`--ai-model <id>` picks another OpenRouter model for a job and `--ai-reasoning
<tokens>` (`ai_framing.reasoning_tokens`) lets it think before it answers;
0 means no extended reasoning. The app offers both next to the engine choice,
with the list of OpenRouter models that accept pictures.

The choice and its reason are kept in the reel plan (`attrs.ai`) and cached,
so planning again asks nobody. If the engine cannot run, the theme's framing
stays and `plan` says why.

### Overlay

`overlay` in the profile (or `--overlay` / `--no-overlay`) draws a speed dial,
altitude and vario from `telemetry.json`. Speed is ground speed. Altitude is
the log's GPS altitude (an IGC from a phone is about 30 m above the GPS
remote's value: ellipsoid against sea level).

## Holiday profile

Many short clips from a selfie stick, several days in one folder.

- Day = the local date in the file name, with the boundary at `day_starts_at_h`.
- The gravity sensor drops stretches with the camera lowered and levels every
  scene; the first and last seconds of a clip are cut.
- Scene types: `view` (walking), `walk` (long walking, sped up and silent),
  `holder` (speech), `panorama` (standing still, slow turn), `companion`
  (a recognised person, stage 6).
- `--mode day` makes one film per day, `--mode trip` one film of everything
  with a share per day, `--mode both` both, from the same analysis.
- Original sound with a short fade at every cut.
- A further theme in the profile (`holiday_surprise`, "Zaskocz mnie") makes one
  more film of the whole trip: small planet, round ultra-wide views, long
  walks as a time-lapse.

With the Media SDK stitcher the clips are stabilised (FlowState with direction
lock). With the FFmpeg stitch scenes are only levelled once per scene. In both
cases the walking direction is assumed to be the lens
facing away from the holder (there is no GPS or optical-flow estimate yet).

## People (Immich)

```powershell
$env:IMMICH_API_KEY = "..."
insta-roller faces-export --immich-url http://nas:2283 --api-key-env IMMICH_API_KEY --people "Ania,Emil" --out people.json
insta-roller faces-check people.json
```

`faces-export` copies the face vectors of the named people out of Immich
(read only; `--db-url` reads them straight from its database, the API path
re-embeds sample photos with the same `buffalo_l` model). Needs
`pip install -e ".[faces]"`.

Put `people.json` in the job folder. The holiday analysis then looks for
those people in every scene: a scene with a known person scores higher, a
walking scene becomes a `companion` shot aimed at them, a group is framed
together and screen time is balanced with the weights in `people:` of the
profile. Strangers are never a target. Without the file or the model the films
are made as before.

## Stitcher, queue and UI

- `stitcher/README.md`: building the Media SDK stitcher. Once
  `insta-stitcher.exe` exists under `stitcher/build` (or `INSTA_ROLLER_STITCHER`
  points at it) rendering uses it for the spans of each shot, with a
  size-limited cache (`--cache-gb`); `tools/bench_stitch.py` measures time and
  GPU memory into `docs/stitch-benchmark.md`.
- `app/README.md`: the application, one process on the Windows PC started with
  `python -m app`, `insta-roller.bat` or a Start menu shortcut: its own window
  (WebView2), tray icon, job queue, job runner and the UI (overview, new film,
  films with the scene review, people, settings). `--browser` shows the same UI
  in a browser.

## Data contracts

`scenes.json`, `telemetry.json`, `reels.json`, `reel_plan_*.json`,
`framing_plan_*.json` and `job_state.json` are the only interface between the
steps and to the UI. `insta-roller schemas <folder>` exports their schemas.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

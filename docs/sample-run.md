# Sample run

Flight of 24 June 2026: winch launch, 3 h 08 min in the log, 2 h 29 min recorded (the camera stopped before landing). Source: LRV proxy, five files joined. Whole run from folder to 45 s reel: 53 s on an RTX 3070 Ti.

```
insta-roller run <folder> --out work\flight --offset 14
```

## Scenes

```
Recording 20260624_125012 (dual_fisheye), 2:28:38, starts 10:50:26 UTC (camera clock + manual offset)
IGC 2026-06-24-XNA-230596-02.igc: 10:57:17 - 14:05:56 UTC, 54 phases

  0:08:17 - 0:08:42  takeoff  score 1.00  
  0:08:42 - 0:09:04  tow      score 0.50  alt_gain_m=427
  0:08:58 - 0:09:10  release  score 0.80  alt_m=606
  0:09:04 - 0:29:18  thermal  score 0.53  direction=left, turns=42.7, climb_ms=0.65, alt_gain_m=795
  0:29:18 - 0:31:01  glide    score 0.50  alt_loss_m=-41, speed_kmh=81
  0:31:01 - 0:34:18  thermal  score 0.71  direction=left, turns=5.8, climb_ms=1.55, alt_gain_m=305
  0:34:18 - 0:38:29  glide    score 0.50  alt_loss_m=155, speed_kmh=87
  0:38:29 - 0:41:01  thermal  score 0.58  direction=left, turns=4.7, climb_ms=0.89, alt_gain_m=135
  0:41:01 - 0:48:08  glide    score 0.50  alt_loss_m=25, speed_kmh=82
  0:48:08 - 0:49:36  thermal  score 0.43  direction=right, turns=2.9, climb_ms=0.14, alt_gain_m=12
  0:49:36 - 0:54:13  glide    score 0.50  alt_loss_m=226, speed_kmh=104
  0:54:13 - 0:57:59  thermal  score 0.48  direction=left, turns=6.8, climb_ms=0.41, alt_gain_m=92
  0:57:59 - 1:00:23  glide    score 0.50  alt_loss_m=68, speed_kmh=117
  1:00:23 - 1:05:33  thermal  score 0.54  direction=left, turns=10.5, climb_ms=0.71, alt_gain_m=220
  1:05:33 - 1:07:05  glide    score 0.50  alt_loss_m=89, speed_kmh=102
  1:07:05 - 1:09:26  thermal  score 0.63  direction=right, turns=4.1, climb_ms=1.13, alt_gain_m=159
  1:09:26 - 1:13:30  glide    score 0.50  alt_loss_m=251, speed_kmh=94
  1:13:30 - 1:14:18  thermal  score 0.40  direction=right, turns=1.2, climb_ms=-0.29, alt_gain_m=-14
  1:14:18 - 1:15:02  glide    score 0.50  alt_loss_m=12, speed_kmh=88
  1:15:02 - 1:18:37  thermal  score 0.67  direction=left, turns=6.8, climb_ms=1.34, alt_gain_m=288
  1:18:37 - 1:22:11  glide    score 0.50  alt_loss_m=176, speed_kmh=132
  1:22:11 - 1:25:42  thermal  score 0.43  direction=right, turns=6.1, climb_ms=0.13, alt_gain_m=28
  1:25:42 - 1:28:32  glide    score 0.50  alt_loss_m=192, speed_kmh=103
  1:28:32 - 1:31:03  thermal  score 0.41  direction=left, turns=3.8, climb_ms=0.03, alt_gain_m=4
  1:31:03 - 1:32:19  glide    score 0.50  alt_loss_m=-4, speed_kmh=95
  1:32:19 - 1:37:09  thermal  score 0.45  direction=left, turns=7.0, climb_ms=0.24, alt_gain_m=70
  1:37:09 - 1:38:12  glide    score 0.50  alt_loss_m=-71, speed_kmh=88
  1:38:12 - 1:41:25  thermal  score 0.55  direction=left, turns=6.8, climb_ms=0.73, alt_gain_m=140
  1:41:25 - 1:44:03  glide    score 0.50  alt_loss_m=127, speed_kmh=90
  1:44:03 - 1:45:41  thermal  score 0.48  direction=right, turns=2.9, climb_ms=0.41, alt_gain_m=40
  1:45:41 - 1:49:29  glide    score 0.50  alt_loss_m=153, speed_kmh=88
  1:49:29 - 1:52:34  thermal  score 0.55  direction=right, turns=5.9, climb_ms=0.74, alt_gain_m=136
  1:52:34 - 1:53:36  glide    score 0.50  alt_loss_m=40, speed_kmh=89
  1:53:36 - 1:57:05  thermal  score 0.55  direction=left, turns=7.2, climb_ms=0.76, alt_gain_m=159
  1:57:05 - 1:59:50  glide    score 0.50  alt_loss_m=140, speed_kmh=104
  1:59:50 - 2:02:57  thermal  score 0.54  direction=left, turns=5.5, climb_ms=0.7, alt_gain_m=130
  2:02:57 - 2:07:46  glide    score 0.50  alt_loss_m=176, speed_kmh=92
  2:07:46 - 2:10:15  thermal  score 0.68  direction=right, turns=4.8, climb_ms=1.41, alt_gain_m=210
  2:10:15 - 2:12:40  glide    score 0.50  alt_loss_m=129, speed_kmh=105
  2:12:40 - 2:16:53  thermal  score 0.53  direction=left, turns=8.8, climb_ms=0.67, alt_gain_m=169
  2:16:53 - 2:22:22  glide    score 0.50  alt_loss_m=207, speed_kmh=113
  2:22:22 - 2:25:30  thermal  score 0.58  direction=left, turns=6.2, climb_ms=0.9, alt_gain_m=170
  2:25:30 - 2:28:38  glide    score 0.50  alt_loss_m=533, speed_kmh=108, truncated=True

Not covered by the recording: circuit, landing
```

## Reel plan

```
plan: 9 shots, 45.0s (target 45s) -> framing_plan_flight_summary_45s.json
   1. 0:08:17    6.3s  takeoff  front
   2. 0:08:51    3.3s  tow      front
   3. 0:09:02    4.9s  release  front -> pilot
   4. 0:30:07    4.4s  glide    pilot_wide -> front
   5. 0:32:37    5.9s  thermal  back_left -> pilot_wide
   6. 0:36:21    4.4s  glide    pilot_wide -> front
   7. 1:16:47    5.8s  thermal  back_left -> pilot_wide
   8. 2:08:58    5.9s  thermal  back_right -> pilot_wide
   9. 2:28:34    4.0s  ending   pilot_wide
```


# Stages 2-5, run of 23 November 2026

## Flight of 23 June 2026 (camera files with GPS remote data)

Winch launch, 2 h 20 min in the log, 2 h 24 min recorded in five files; the
recording ends about 7 minutes before the landing. No manual offset.

```
insta-roller run <folder> --out work/flight2 --shots
```

```
Recording 20260623_114648 (dual_fisheye), 2:23:38, starts 09:47:00 UTC (gyro turns matched to the track, correlation 0.81)
Scenes from: IGC 2026-06-23-XNA-230596-01.igc: 09:59:42 - 12:19:53 UTC, 41 phases
plan: takeoff_tow_20s: 3 shots, 20.0s
plan: thermal_30s: 4 shots, 30.0s
plan: glide_views_25s: 4 shots, 25.0s
plan: flight_summary_45s: 9 shots, 45.0s
plan: landing_20s: no reel, theme is switched off
```

- Camera clock: 09:46:48. GPS remote in the file: 09:46:58. IGC (phone): 09:47:00.
- The same flight with `--no-igc` (camera GPS only) finds the same scenes
  within one second; start 09:46:58, correlation 0.82.
- Flight of 24 June (no GPS in the camera): automatic sync gives 10:50:25,
  the offset set by eye in stage 1 gave 10:50:26. Correlation 0.90.
  With `--no-igc` it falls back to the gyroscope: the thermals come out with
  the right direction and times, the ground time before takeoff shows as a glide.
- Source: full-quality INSV (2 x 2880 x 2880 HEVC), rough FFmpeg stitch.
  The 45 s summary took 3 min 49 s on the RTX 3070 Ti while another render was
  running. From the LRV proxy the same reel takes under a minute.

## Holiday: eight clips of 12 and 14 August 2025 (8K INSV, no LRV)

```
insta-roller run work/holiday-src --out work/holiday --mode both --length 30
```

Eight clips on two days give `day_20250812_30s`, `day_20250814_30s` and
`trip_180s`. Films are 1920 x 1080 with the original sound. Rendering 8K
sources runs at roughly a quarter of real time.

## Media SDK stitcher (added the same day)

Built with Visual Studio 2022 Build Tools; `insta-stitcher.exe` stitches only
the spans a reel uses.

- 5.7K flight, no stabilisation, native 5760 x 2880: about 15 s per second of
  footage (6 s span in 90 s). The 45 s summary reel: 13 min 15 s.
- 8K holiday clip, direction lock, 3840 x 1920 output: about 17-22 s per
  second of footage. Native 7680 x 3840: about 26 s per second.
- No seam and no doubled objects; the mount calibration of the proxy fits
  the SDK picture unchanged.

## People from Immich

`faces-export` against Immich 3.4.2 (API path) exported 50 face vectors for
each of four people. On three clips from 2 August 2025 the analysis found two
of them and framed them as `companion` shots.

## Standalone app

`python -m app` (or `insta-roller.bat`): one process with the queue, the job
runner and the browser UI. A job for the 23 June flight (LRV + IGC, review
on) reached the scene review by itself and, after "Renderuj", finished with
two reels listed on the results screen.

## Whole cruise, day films (23 November 2026, evening)

88 clips (8K INSV, no LRV) on eight days, people from Immich, Media SDK
stitch with direction lock at 3840 x 1920.

- Analysis with people recognition: about 1 h 10 min (125 scenes, three
  moments each). 58 scenes became `companion` shots.
- Render: eight day films in about 2 h 35 min, roughly 20 min per 60 s film.
- Two days gave less than 60 s (42 s and 49 s) for lack of usable footage.
- Seen on contact sheets: level and stable; known people are found, but the
  frame often cuts the top of a head because only the direction of a face is
  used, not its height, and the holder dominates the films.

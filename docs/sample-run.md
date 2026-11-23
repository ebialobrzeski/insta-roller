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

# Stitcher benchmark

Rows are appended by `tools/bench_stitch.py`. One row is one run of
`insta-stitcher` on a span of a camera file.

- **Realtime factor** = span length / wall time (1.0 = as fast as playback, higher is faster).
- **Peak VRAM** is the highest `memory.used` of the GPU while the stitcher ran, sampled every
  0.5 s with `nvidia-smi`; it includes whatever else was using the GPU (**VRAM before**).
- Wall time covers the whole process: SDK start-up, stitching, and for partial spans the
  FFmpeg encode of the exported frames.


**Recommendation:** the RTX 3070 Ti is enough as the worker. Peak GPU memory was 2.4 GB for 5.7K
and 3.4 GB for 8K (including about 1.4 GB used by the desktop); the limit is time, not memory.
The RTX 4080 has not been measured.

| Date | GPU | Input | Input streams | Output | Stabilize | Trim mode | Span s | Wall s | Realtime factor | Peak VRAM MiB | VRAM before MiB | Peak GPU % | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-23 16:35 | NVIDIA GeForce RTX 3070 Ti (8 GB) | VID_20260623_114648_00_001.insv | 2880x2880 + 2880x2880 | 5760x2880 | none | frames | 10 | 144.8 | 0.07 | 2428 | 1373 | 67 |  |
| 2026-11-23 16:40 | NVIDIA GeForce RTX 3070 Ti (8 GB) | VID_20250814_132809_00_012.insv | 3840x3840 + 3840x3840 | 7680x3840 | direction-lock | frames | 10 | 242.7 | 0.04 | 3398 | 1595 | 47 |  |

## Quick stitch (2026-11-25)

The table above is the SDK's frame-by-frame export, the only way it renders a span: about
15–24 s of work per second of footage. Its video export of a whole file takes about 0.6–0.8 s
per second. `core/stitch.py` therefore cuts the span out of the camera file without re-encoding,
puts the camera's own data (`inst` box) back at the end, and lets the SDK export that short file.

- Unstabilised mounts (glider): the cut is exactly the span. Frame-accurate against the old output.
- Stabilised mounts (selfie stick): the gyroscope counts from the start of the file, so the cut
  runs from 0 to the last shot wanted from that clip; clips up to 60 s are stitched whole and
  shared by all their shots. A short span far into a long file still goes frame by frame.

Whole reel from empty caches, RTX 3070 Ti, two shots at once:

| Reel | Footage | Before | After |
| --- | --- | --- | --- |
| Cruise day, 12 shots, 8K, direction-lock, 3840 wide | 60 s | about 22 min (stitch 21 s per second) | 3 min 26 s |
| Glider flight summary, 9 shots, 5.7K, no stabilisation | 45 s | about 12 min (stitch 15 s per second) | 2 min 28 s |

What is left is mostly FFmpeg's `v360` on the processor; the graphics card is no longer the limit.

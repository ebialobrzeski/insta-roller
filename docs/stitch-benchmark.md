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

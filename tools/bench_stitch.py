"""Benchmark of the Media SDK stitcher (`stitcher/`) on one camera file.

Stitches a span of an .insv with insta-stitcher, samples nvidia-smi every
0.5 s while it runs and appends one row to docs/stitch-benchmark.md.

    .venv\\Scripts\\python.exe tools\\bench_stitch.py C:\\footage\\VID_..._00_001.insv
    .venv\\Scripts\\python.exe tools\\bench_stitch.py file.insv --start 60 --span 20 --stabilize none

The input is only read. The stitched file goes to work/stitch-test/.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import ffmpeg, stitch  # noqa: E402

REPORT = ROOT / "docs" / "stitch-benchmark.md"
COLUMNS = ("Date", "GPU", "Input", "Input streams", "Output", "Stabilize", "Trim mode", "Span s",
           "Wall s", "Realtime factor", "Peak VRAM MiB", "VRAM before MiB", "Peak GPU %", "Notes")
HEADER = f"""# Stitcher benchmark

Rows are appended by `tools/bench_stitch.py`. One row is one run of
`insta-stitcher` on a span of a camera file.

- **Realtime factor** = span length / wall time (1.0 = as fast as playback, higher is faster).
- **Peak VRAM** is the highest `memory.used` of the GPU while the stitcher ran, sampled every
  0.5 s with `nvidia-smi`; it includes whatever else was using the GPU (**VRAM before**).
- Wall time covers the whole process: SDK start-up, stitching, and for partial spans the
  FFmpeg encode of the exported frames.

| {" | ".join(COLUMNS)} |
|{"|".join(" --- " for _ in COLUMNS)}|
"""
PLACEHOLDER = "No measurement exists yet"


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"bench_stitch: {message}", file=sys.stderr)
    raise SystemExit(1)


def nvidia_smi(query: str, gpu: int) -> list[str] | None:
    """One CSV line of `nvidia-smi --query-gpu`, split into fields; None when it cannot be run."""
    try:
        res = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits", "-i", str(gpu)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line for line in res.stdout.splitlines() if line.strip()]
    if res.returncode != 0 or not lines:
        return None
    return [field.strip() for field in lines[0].split(",")]


class GpuSampler(threading.Thread):
    """Samples memory.used and utilization.gpu until stopped."""

    def __init__(self, gpu: int, interval_s: float = 0.5):
        super().__init__(daemon=True)
        self.gpu, self.interval_s = gpu, interval_s
        self.samples: list[tuple[int, int]] = []
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            began = time.monotonic()
            fields = nvidia_smi("memory.used,utilization.gpu", self.gpu)
            try:
                if fields:
                    self.samples.append((int(float(fields[0])), int(float(fields[1]))))
            except (ValueError, IndexError):
                pass
            self._halt.wait(max(0.0, self.interval_s - (time.monotonic() - began)))

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=15)


def append_row(row: dict[str, str]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    text = REPORT.read_text(encoding="utf-8") if REPORT.exists() else HEADER
    if "| Date |" not in text:
        text = HEADER
    # drop the "no measurement yet" note once there is a real row
    text = "\n".join(line for line in text.splitlines() if PLACEHOLDER not in line).rstrip("\n") + "\n"
    text += "| " + " | ".join(row[c].replace("|", "/") for c in COLUMNS) + " |\n"
    REPORT.write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="camera file (.insv)")
    ap.add_argument("--start", type=float, default=0.0, help="start of the span in seconds (default 0)")
    ap.add_argument("--span", type=float, default=20.0, help="length of the span in seconds (default 20)")
    ap.add_argument("--stabilize", default="direction-lock", choices=("none", "flowstate", "direction-lock"))
    ap.add_argument("--width", type=int, default=0, help="output width, height is width/2 (default: native)")
    ap.add_argument("--trim-mode", default="auto", choices=("auto", "frames", "full"))
    ap.add_argument("--gpu", type=int, default=0, help="GPU index for nvidia-smi and the stitcher (default 0)")
    ap.add_argument("--exe", type=Path, help="insta-stitcher executable (default: as core.stitch finds it)")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "work" / "stitch-test")
    ap.add_argument("--notes", default="", help="free text for the Notes column")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[],
                    help="everything after --extra is passed to insta-stitcher unchanged")
    args = ap.parse_args()

    exe = args.exe or stitch.find_exe()
    if exe is None or not Path(exe).exists():
        fail("insta-stitcher is not built. Build it first (see stitcher/README.md), e.g.\n"
             "    cmake -S stitcher -B stitcher/build -A x64 -DINSTA_MEDIA_SDK_DIR=<MediaSDK folder>\n"
             "    cmake --build stitcher/build --config Release\n"
             "or point INSTA_ROLLER_STITCHER / --exe at an existing insta-stitcher executable.")
    if not args.input.is_file():
        fail(f"input file not found: {args.input}")
    if args.span <= 0 or args.start < 0:
        fail("--span must be positive and --start must not be negative")

    source = ffmpeg.probe(args.input)
    if args.start + args.span > source.duration_s + 0.05:
        fail(f"span {args.start:g}s+{args.span:g}s does not fit in the file ({source.duration_s:.1f} s)")
    streams = " + ".join(f"{w}x{h}" for w, h in source.video_sizes) or "?"

    name = nvidia_smi("name,memory.total", args.gpu)
    if name is None:
        fail("nvidia-smi could not be run or reported no GPU; the benchmark needs it for the VRAM figures")
    gpu_name = f"{name[0]} ({int(float(name[1])) // 1024} GB)"
    before = nvidia_smi("memory.used", args.gpu)
    vram_before = int(float(before[0])) if before else 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / (f"bench_{args.input.stem}_{int(args.start * 1000)}_{int(args.span * 1000)}"
                          f"_{args.stabilize}_{args.width or 'native'}.mp4")
    out.unlink(missing_ok=True)
    cmd = [str(exe), "--input", str(args.input), "--output", str(out),
           "--start-ms", str(int(round(args.start * 1000))), "--duration-ms", str(int(round(args.span * 1000))),
           "--stabilize", args.stabilize, "--trim-mode", args.trim_mode, "--gpu", str(args.gpu)]
    if args.width:
        cmd += ["--width", str(args.width), "--height", str(args.width // 2)]
    cmd += args.extra
    env = dict(os.environ)
    env.setdefault("INSTA_ROLLER_FFMPEG", ffmpeg.ffmpeg_exe())      # partial spans are encoded by FFmpeg

    print(subprocess.list2cmdline(cmd))
    sampler = GpuSampler(args.gpu)
    sampler.start()
    began = time.perf_counter()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    finally:
        wall = time.perf_counter() - began
        sampler.stop()
    if res.returncode != 0 or not out.exists():
        fail(f"insta-stitcher failed (exit {res.returncode}):\n{(res.stderr or res.stdout)[-2000:]}")

    result = ffmpeg.probe(out)
    out_size = "x".join(map(str, result.video_sizes[0])) if result.video_sizes else "?"
    mode = next((line.split("mode ")[1].split(",")[0] for line in res.stderr.splitlines()
                 if "insta-stitcher: SDK " in line and "mode " in line), args.trim_mode)
    peak_vram = max((m for m, _ in sampler.samples), default=0)
    peak_util = max((u for _, u in sampler.samples), default=0)
    notes = [args.notes] if args.notes else []
    if abs(result.duration_s - args.span) > 0.1:
        notes.append(f"output is {result.duration_s:.2f} s long")
    if not sampler.samples:
        notes.append("no nvidia-smi samples")

    row = {
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "GPU": gpu_name,
        "Input": args.input.name,
        "Input streams": streams,
        "Output": out_size,
        "Stabilize": args.stabilize,
        "Trim mode": mode,
        "Span s": f"{args.span:g}",
        "Wall s": f"{wall:.1f}",
        "Realtime factor": f"{args.span / wall:.2f}",
        "Peak VRAM MiB": str(peak_vram),
        "VRAM before MiB": str(vram_before),
        "Peak GPU %": str(peak_util),
        "Notes": "; ".join(notes),
    }
    append_row(row)
    for key in COLUMNS:
        print(f"{key:>16}: {row[key]}")
    print(f"row appended to {REPORT}")


if __name__ == "__main__":
    main()

"""Full-quality stitching through the Media SDK command line tool (`stitcher/`).

Only the spans a reel uses are stitched. The results live in a local cache
with a size limit; the least recently used segments are deleted first.
The rest of the application sees a stitched segment as an ordinary
equirectangular recording.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import ffmpeg
from .ingest import Recording, fingerprint

ROOT = Path(__file__).resolve().parent.parent
EXE_NAMES = ("insta-stitcher.exe", "insta-stitcher")


def find_exe() -> Path | None:
    """INSTA_ROLLER_STITCHER, or a build under stitcher/build."""
    env = os.environ.get("INSTA_ROLLER_STITCHER")
    if env:
        return Path(env) if Path(env).exists() else None
    for name in EXE_NAMES:
        for hit in sorted((ROOT / "stitcher" / "build").glob(f"**/{name}")):
            return hit
    return None


def prune(folder: Path, limit_bytes: float, keep: set[Path] = frozenset()) -> list[Path]:
    """Deletes the least recently used files until the folder fits the limit."""
    files = sorted((p for p in folder.glob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = []
    for p in files:
        if total <= limit_bytes:
            break
        if p in keep:
            continue
        total -= p.stat().st_size
        p.unlink()
        removed.append(p)
    return removed


def localize(path: Path, folder: Path, limit_bytes: float) -> Path:
    """A copy of a source file on the fast local disk (the originals stay on the NAS)."""
    folder.mkdir(parents=True, exist_ok=True)
    st = path.stat()
    local = folder / f"{fingerprint(str(path.parent))}_{path.name}"
    if not (local.exists() and local.stat().st_size == st.st_size):
        tmp = local.with_suffix(local.suffix + ".part")
        shutil.copyfile(path, tmp)
        tmp.replace(local)
    local.touch()
    prune(folder, limit_bytes, {local})
    return local


class Stitcher:
    def __init__(self, exe: Path, cache: Path, limit_gb: float = 50.0, width: int = 0,
                 stabilize: str = "direction-lock", local_sources: bool = False):
        self.exe, self.cache, self.limit = Path(exe), Path(cache), limit_gb * 1e9
        self.width, self.stabilize, self.local_sources = width, stabilize, local_sources

    def _run(self, source: Path, start_s: float, duration_s: float, out: Path, log: Path) -> None:
        cmd = [str(self.exe), "--input", str(source), "--output", str(out),
               "--start-ms", str(int(round(start_s * 1000))), "--duration-ms", str(int(round(duration_s * 1000))),
               "--stabilize", self.stabilize]
        if self.width:
            cmd += ["--width", str(self.width), "--height", str(self.width // 2)]
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {subprocess.list2cmdline(cmd)}\n")
        began = time.time()
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env={**os.environ, "INSTA_ROLLER_FFMPEG": ffmpeg.ffmpeg_exe()})
        if res.returncode != 0 or not out.exists():
            raise RuntimeError(f"stitcher failed ({res.returncode}):\n{(res.stderr or res.stdout)[-800:]}")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"    stitched {duration_s:.1f}s in {time.time() - began:.1f}s\n")

    def segment(self, rec: Recording, start_s: float, duration_s: float, log: Path,
                stabilize: str | None = None, width: int | None = None) -> Recording:
        """The span [start_s, start_s + duration_s) of a camera take as an equirectangular recording."""
        self.cache.mkdir(parents=True, exist_ok=True)
        self.stabilize = stabilize or self.stabilize
        self.width = width or self.width
        key = fingerprint(rec.id, *rec.files, f"{start_s:.3f}", f"{duration_s:.3f}", self.width, self.stabilize)
        out = self.cache / f"{key}.mp4"
        if not out.exists():
            parts, offset = [], 0.0
            for path, length in zip(rec.files, rec.durations):       # a span may cross a file boundary
                a, b = max(start_s, offset), min(start_s + duration_s, offset + length)
                if b - a > 0.02:
                    part = self.cache / f"{key}_{len(parts)}.part.mp4"
                    source = localize(path, self.cache / "sources", self.limit) if self.local_sources else path
                    self._run(source, a - offset, b - a, part, log)
                    parts.append(part)
                offset += length
            if not parts:
                raise RuntimeError(f"span {start_s:.1f}s+{duration_s:.1f}s lies outside recording {rec.id}")
            if len(parts) == 1:
                parts[0].replace(out)
            else:
                listing = self.cache / f"{key}.txt"
                listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
                ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)], log=log)
                for p in (*parts, listing):
                    p.unlink()
        out.touch()
        prune(self.cache, self.limit, {out})
        info = ffmpeg.probe(out)
        return Recording(f"{rec.id}_{key}", "sdk", [out], [info.duration_s], None, info.video_sizes[0])


def make_proxy(rec: Recording, folder: Path, log: Path, width: int = 1664) -> Recording:
    """A small dual-fisheye working copy of a take the camera stored without an LRV file."""
    folder.mkdir(parents=True, exist_ok=True)
    files, durations = [], []
    for path in rec.files:
        out = folder / f"{path.stem}_{fingerprint(path)}.mp4"
        if not out.exists():
            tmp = out.with_suffix(".part.mp4")
            half = width // 2
            ffmpeg.run(["-i", str(path), "-filter_complex",
                        f"[0:v:0]scale={half}:{half}[a];[0:v:1]scale={half}:{half}[b];[a][b]hstack=inputs=2[v]",
                        "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                        "-g", "30", "-c:a", "aac", "-b:a", "96k", str(tmp)], log=log)
            tmp.replace(out)
        files.append(out)
        durations.append(ffmpeg.probe(out).duration_s)
    proxy = Recording(rec.id, "dual_fisheye", files, durations, rec.start_utc, (width, width // 2))
    proxy.alt = {"insv": rec}
    return proxy

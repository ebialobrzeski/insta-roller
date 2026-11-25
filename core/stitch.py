"""Full-quality stitching through the Media SDK command line tool (`stitcher/`).

Only the spans a reel uses are stitched. The results live in a local cache
with a size limit; the least recently used segments are deleted first.
The rest of the application sees a stitched segment as an ordinary
equirectangular recording.

The SDK exports video about twenty times quicker than single frames, but only of a
whole file: it has no call for a time span. So a span is first cut out of the camera
file without re-encoding (see cut_camera_file) and the SDK stitches that short file.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import threading
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
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError:                 # in use by a shot being rendered right now
            continue
        total -= size
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


def camera_trailer(path: Path) -> tuple[int, int] | None:
    """(offset, size) of the camera's own data at the end of an .insv file: the top-level
    box `inst`, which holds the lens calibration and the gyroscope samples the SDK needs."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            length, kind = struct.unpack(">I4s", f.read(8))
            if length == 1:
                length = struct.unpack(">Q", f.read(8))[0]
            elif length == 0:
                length = size - pos
            if length < 8:
                return None
            if kind == b"inst":
                return pos, min(length, size - pos)
            pos += length
    return None


def cut_camera_file(source: Path, start_s: float, duration_s: float, out: Path, log: Path) -> bool:
    """A camera file that holds only [start_s, start_s + duration_s) of `source`: the pictures and
    the sound are copied as they are, and the camera's own data is put back at the end. Takes a
    moment and loses nothing. The gyroscope samples still count from the start of the original,
    so a stabilised stitch is only right for a cut that starts at 0. False when the file has no
    camera data to carry over."""
    trailer = camera_trailer(source)
    if trailer is None:
        return False
    video = out.with_suffix(".video.mp4")
    seek = ["-ss", f"{start_s:.3f}"] if start_s > 0.02 else []
    ffmpeg.run([*seek, "-i", str(source), "-t", f"{duration_s:.3f}", "-map", "0:v", "-map", "0:a?", "-c", "copy",
                "-f", "mp4", str(video)], log=log)
    with open(out, "wb") as target, open(video, "rb") as pictures, open(source, "rb") as camera:
        shutil.copyfileobj(pictures, target, 1 << 24)
        camera.seek(trailer[0])
        left = trailer[1]
        while left > 0:
            chunk = camera.read(min(left, 1 << 24))
            if not chunk:
                break
            target.write(chunk)
            left -= len(chunk)
    video.unlink()
    return True


FRAMES_COST = 25.0          # stitching a span frame by frame costs this many times a whole-file export of the same length
SHORT_FILE_S = 60.0         # a stabilised clip this short is stitched whole, so that every shot from it shares one file


class Stitcher:
    def __init__(self, exe: Path, cache: Path, limit_gb: float = 50.0, width: int = 0,
                 stabilize: str = "direction-lock", local_sources: bool = False):
        self.exe, self.cache, self.limit = Path(exe), Path(cache).resolve(), limit_gb * 1e9
        self.width, self.stabilize, self.local_sources = width, stabilize, local_sources
        self.wanted: dict[Path, float] = {}             # camera file -> the latest moment a shot needs from it
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()

    def want(self, rec: Recording, start_s: float, duration_s: float) -> None:
        """Tell the stitcher ahead what will be asked for, so that shots from one stabilised clip
        are served by a single stitch that reaches the last of them."""
        offset = 0.0
        for path, length in zip(rec.files, rec.durations):
            a, b = max(start_s, offset), min(start_s + duration_s, offset + length)
            if b - a > 0.02:
                self.wanted[path] = max(self.wanted.get(path, 0.0), b - offset)
            offset += length

    def _lock(self, name: str) -> threading.Lock:
        with self.guard:
            return self.locks.setdefault(name, threading.Lock())

    def _options(self, stabilize: str, width: int) -> list[str]:
        return ["--stabilize", stabilize] + (["--width", str(width), "--height", str(width // 2)] if width else [])

    def _part(self, path: Path, length: float, start_s: float, duration_s: float, stabilize: str, width: int,
              log: Path) -> tuple[Path, float]:
        """(stitched file, where the span begins in it) for a span inside one camera file."""
        source = localize(path, self.cache / "sources", self.limit) if self.local_sources else path
        end = start_s + duration_s
        if stabilize == "none":                         # nothing depends on the clock: cut exactly the span
            a, b = start_s, end
        else:                                           # the gyroscope counts from the start of the file: cut from 0
            a, b = 0.0, max(end, self.wanted.get(path, 0.0))
            if length <= SHORT_FILE_S:
                b = length
        whole = a < 0.02 and b > length - 0.05
        if not whole and b - a > duration_s * FRAMES_COST:      # a short span late in a long stabilised file
            out = self.cache / f"{fingerprint(path, f'{start_s:.3f}', f'{duration_s:.3f}', width, stabilize)}.mp4"
            with self._lock(out.name):
                if not out.exists():
                    self._run(source, out, log, ["--start-ms", str(int(round(start_s * 1000))),
                                                 "--duration-ms", str(int(round(duration_s * 1000))),
                                                 *self._options(stabilize, width)], duration_s)
            return out, 0.0
        family = fingerprint(path, width, stabilize, "from the start" if a < 0.02 else f"{a:.3f}")
        with self._lock(family):
            for have in self.cache.glob(f"{family}_*.mp4"):          # one that reaches far enough is as good
                reach = have.stem.rsplit("_", 1)[1]
                if reach.isdigit() and int(reach) >= int(round(b * 1000)) - 20:
                    return have, self._lead(have, a, int(reach) / 1000, start_s)
            out = self.cache / f"{family}_{int(round(b * 1000))}.mp4"
            if whole:
                self._run(source, out, log, self._options(stabilize, width), length)
            else:
                short = self.cache / f"{family}_{int(round(b * 1000))}.cut.insv"
                try:
                    if not cut_camera_file(source, a, b - a, short, log):
                        raise RuntimeError("no camera data in the file")
                    self._run(short, out, log, self._options(stabilize, width), b - a)
                except RuntimeError as error:           # an unusual file: the slow way still works
                    with open(log, "a", encoding="utf-8") as f:
                        f.write(f"    quick stitch failed ({str(error)[:200]}), stitching frame by frame\n")
                    out = self.cache / f"{fingerprint(path, f'{start_s:.3f}', f'{duration_s:.3f}', width, stabilize)}.mp4"
                    self._run(source, out, log, ["--start-ms", str(int(round(start_s * 1000))),
                                                 "--duration-ms", str(int(round(duration_s * 1000))),
                                                 *self._options(stabilize, width)], duration_s)
                    return out, 0.0
                finally:
                    short.unlink(missing_ok=True)
            return out, self._lead(out, a, b, start_s)

    @staticmethod
    def _lead(out: Path, a: float, b: float, start_s: float) -> float:
        """Where start_s is in a stitch of [a, b). A cut begins at the keyframe before `a`; when
        the SDK kept those extra frames, the file is that much longer than asked for."""
        extra = ffmpeg.probe(out).duration_s - (b - a)
        return max(0.0, start_s - a + (extra if 0.05 < extra < 10 else 0.0))

    def _run(self, source: Path, out: Path, log: Path, options: list[str], duration_s: float) -> None:
        tmp = out.with_suffix(".part.mp4")
        cmd = [str(self.exe), "--input", str(source), "--output", str(tmp), *options]
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {subprocess.list2cmdline(cmd)}\n")
        began = time.time()
        print(f"render: stitching {duration_s:.0f} s of footage", flush=True)
        res = subprocess.run(cmd, capture_output=True, **ffmpeg.QUIET, text=True, encoding="utf-8", errors="replace",
                             env={**os.environ, "INSTA_ROLLER_FFMPEG": ffmpeg.ffmpeg_exe()})
        if res.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"stitcher failed ({res.returncode}):\n{(res.stderr or res.stdout)[-800:]}")
        tmp.replace(out)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"    stitched {duration_s:.1f}s in {time.time() - began:.1f}s\n")

    def segment(self, rec: Recording, start_s: float, duration_s: float, log: Path,
                stabilize: str | None = None, width: int | None = None) -> Recording:
        """The span [start_s, start_s + duration_s) of a camera take as an equirectangular recording.
        The span begins `lead_s` into that recording: the stitched file may hold more than was asked."""
        self.cache.mkdir(parents=True, exist_ok=True)
        stabilize, width = stabilize or self.stabilize, width or self.width
        parts, offset = [], 0.0
        for path, length in zip(rec.files, rec.durations):           # a span may cross a file boundary
            a, b = max(start_s, offset), min(start_s + duration_s, offset + length)
            if b - a > 0.02:
                parts.append((*self._part(path, length, a - offset, b - a, stabilize, width, log), b - a))
            offset += length
        if not parts:
            raise RuntimeError(f"span {start_s:.1f}s+{duration_s:.1f}s lies outside recording {rec.id}")
        if len(parts) == 1:
            out, lead = parts[0][0], parts[0][1]
        else:                                                           # the pieces, each trimmed to its share, as one file
            out, lead = self.cache / f"{fingerprint(*[str(p) for p in parts])}.mp4", 0.0
            if not out.exists():
                pieces = []
                for n, (path, begin, length) in enumerate(parts):
                    piece = out.with_suffix(f".{n}.piece.mp4")
                    ffmpeg.run(["-ss", f"{begin:.3f}", "-i", str(path), "-t", f"{length:.3f}", "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "14", "-c:a", "aac", "-b:a", "192k", str(piece)], log=log)
                    pieces.append(piece)
                listing = out.with_suffix(".txt")
                listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
                tmp = out.with_suffix(".part.mp4")      # a job stopped while joining leaves no file that looks done
                ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(tmp)], log=log)
                tmp.replace(out)
                for p in (*pieces, listing):
                    p.unlink()
        out.touch()
        prune(self.cache, self.limit, {out, *(p[0] for p in parts)})
        info = ffmpeg.probe(out)
        made = Recording(f"{rec.id}_{out.stem}", "sdk", [out], [info.duration_s], None, info.video_sizes[0])
        made.lead_s = lead
        return made


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

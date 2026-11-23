"""FFmpeg process wrapper. Every command is appended to the job log."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


@lru_cache
def ffmpeg_exe() -> str:
    exe = os.environ.get("INSTA_ROLLER_FFMPEG") or shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError(
            "FFmpeg not found. Install it, set INSTA_ROLLER_FFMPEG, or "
            "`pip install imageio-ffmpeg`.") from None


def run(args: list[str], log: Path | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *args]
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {subprocess.list2cmdline(cmd)}\n")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    if res.returncode != 0:
        tail = "\n".join(res.stderr.strip().splitlines()[-12:])
        raise RuntimeError(f"ffmpeg failed ({res.returncode}):\n{tail}")
    return res


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    creation_utc: float | None
    video_sizes: tuple[tuple[int, int], ...]


def probe(path: Path) -> MediaInfo:
    res = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = res.stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError(f"cannot read duration of {path}")
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    created = None
    c = re.search(r"creation_time\s*:\s*(\S+)", err)
    if c:
        created = datetime.fromisoformat(c[1].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    sizes = tuple((int(w), int(h)) for w, h in re.findall(r"Stream #.*Video:.*?\b(\d{3,5})x(\d{3,5})\b", err))
    return MediaInfo(duration, created, sizes)


@lru_cache
def has_encoder(name: str) -> bool:
    """True when the encoder exists and actually opens (NVENC needs a GPU)."""
    res = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=black:s=256x256:d=0.1", "-c:v", name, "-f", "null", "-"],
        capture_output=True)
    return res.returncode == 0

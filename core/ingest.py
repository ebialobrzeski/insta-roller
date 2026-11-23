"""Find recordings and flight logs in a job folder.

A recording is one continuous take. The camera splits long takes into several
files that share a timestamp; they are joined into one timeline here.

The equirectangular source sits behind one interface with several
implementations: an Insta360 Studio export, the camera's dual-fisheye LRV
proxy (rough stitch, for development), and later the Media SDK stitcher.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .config import Mount

_CAMERA = re.compile(r"^(VID|LRV)_(\d{8}_\d{6})_\d{2}_(\d{3})\.(insv|lrv)$", re.I)


@dataclass
class Recording:
    id: str
    kind: str                    # equirect | dual_fisheye
    files: list[Path]
    durations: list[float]
    start_utc: float | None      # camera clock at the first frame
    size: tuple[int, int]

    @property
    def duration_s(self) -> float:
        return sum(self.durations)

    def input_args(self, work: Path, start_s: float, duration_s: float = 0.0) -> list[str]:
        """FFmpeg input arguments seeked to start_s on the joined timeline.

        Seeking inside one file is fast. Only a span that crosses a file
        boundary goes through the concat demuxer, whose seek is slow.
        """
        offset = 0.0
        for path, length in zip(self.files, self.durations):
            if offset <= start_s and start_s + duration_s <= offset + length:
                return ["-ss", f"{start_s - offset:.3f}", "-i", str(path)]
            offset += length
        listing = work / f"concat_{self.id}.txt"
        work.mkdir(parents=True, exist_ok=True)
        lines = ["file '%s'" % f.as_posix().replace("'", "'\\''") for f in self.files]
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ["-ss", f"{start_s:.3f}", "-f", "concat", "-safe", "0", "-i", str(listing)]

    def to_aircraft_frame(self, mount: Mount) -> str | None:
        """Filter that yields an equirectangular picture in the aircraft frame."""
        cal = mount.calibration.get(self.kind)
        rot = "" if cal is None else f":yaw={cal.yaw}:pitch={cal.pitch}:roll={cal.roll}"
        if self.kind == "dual_fisheye":
            fov = cal.lens_fov if cal and cal.lens_fov else 190
            w, h = self.size
            return f"v360=input=dfisheye:output=e:ih_fov={fov}:iv_fov={fov}:w={w}:h={w // 2}{rot}"
        if cal and (cal.yaw or cal.pitch or cal.roll):
            return f"v360=input=e:output=e{rot}"
        return None


def discover(folder: Path) -> tuple[list[Recording], list[Path]]:
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir() if p.is_file())
    igc = [p for p in files if p.suffix.lower() == ".igc"]

    groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for p in files:
        m = _CAMERA.match(p.name)
        if m and m[4].lower() == "lrv":
            groups.setdefault((m[2], "dual_fisheye"), []).append((int(m[3]), p))
        elif p.suffix.lower() == ".mp4":
            groups.setdefault((p.stem, "equirect"), []).append((0, p))

    recordings = []
    for (rec_id, kind), parts in sorted(groups.items()):
        paths = [p for _, p in sorted(parts)]
        infos = [ffmpeg.probe(p) for p in paths]
        recordings.append(Recording(rec_id, kind, paths, [i.duration_s for i in infos],
                                    infos[0].creation_utc, infos[0].video_sizes[0]))
    return recordings, igc


def fingerprint(*parts) -> str:
    h = hashlib.sha1()
    for part in parts:
        if isinstance(part, Path):
            st = part.stat()
            part = f"{part.name}:{st.st_size}:{int(st.st_mtime)}"
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]

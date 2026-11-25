"""Find recordings and flight logs in a job folder.

A recording is one continuous take. The camera splits long takes into several
files that share a timestamp; they are joined into one timeline here.

The equirectangular source sits behind one interface with several
implementations (`kind`):

- equirect      an Insta360 Studio export,
- sdk           a segment stitched by the Media SDK stitcher (`core/stitch.py`),
- dual_fisheye  the camera's LRV proxy, stitched roughly by FFmpeg (analysis, previews),
- insv          the camera's full-quality file, stitched roughly by FFmpeg when
                the Media SDK stitcher is not available.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg
from .config import Calibration, Mount

_CAMERA = re.compile(r"^(VID|LRV)_(\d{8}_\d{6})_\d{2}_(\d{3})\.(insv|lrv)$", re.I)
EQ = "[eq]"


@dataclass
class Recording:
    id: str
    kind: str                    # equirect | dual_fisheye | insv
    files: list[Path]
    durations: list[float]
    start_utc: float | None      # camera clock at the first frame
    size: tuple[int, int]
    alt: dict[str, "Recording"] = field(default_factory=dict)    # the same take from other files
    lead_s: float = 0.0          # a stitched segment: where the span that was asked for begins in it

    @property
    def duration_s(self) -> float:
        return sum(self.durations)

    def variant(self, kind: str) -> "Recording":
        return self if kind == self.kind else self.alt[kind]

    def best(self) -> "Recording":
        """The sharpest files of this take."""
        return self.alt.get("insv", self) if self.kind == "dual_fisheye" else self

    def locate(self, start_s: float) -> tuple[Path, float]:
        """(file, time inside it) of a moment on the joined timeline."""
        offset = 0.0
        for path, length in zip(self.files, self.durations):
            if start_s < offset + length or path == self.files[-1]:
                return path, start_s - offset
            offset += length
        raise ValueError("empty recording")

    def input_args(self, work: Path, start_s: float, duration_s: float = 0.0, still: bool = False) -> list[str]:
        """FFmpeg input arguments seeked to start_s on the joined timeline.

        Seeking inside one file is fast. Only a span that crosses a file
        boundary goes through the concat demuxer, whose seek is slow.
        still: one frame is wanted and the nearest keyframe will do (planning, thumbnails):
        the graphics card decodes, and only keyframes are decoded. Eight times quicker on 8K.
        """
        quick = STILL_FLAGS if still else []
        offset = 0.0
        for path, length in zip(self.files, self.durations):
            if offset <= start_s and start_s + duration_s <= offset + length + 0.1:
                return [*quick, "-ss", f"{start_s - offset:.3f}", "-i", str(path)]
            offset += length
        listing = work / f"concat_{self.id}_{self.kind}.txt"
        work.mkdir(parents=True, exist_ok=True)
        lines = ["file '%s'" % f.as_posix().replace("'", "'\\''") for f in self.files]
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [*quick, "-ss", f"{start_s:.3f}", "-f", "concat", "-safe", "0", "-i", str(listing)]

    def calibration(self, mount: Mount) -> Calibration | None:
        cal = mount.calibration.get(self.kind)
        if cal is None and self.kind in ("insv", "sdk"):   # same lens layout as the proxy
            cal = mount.calibration.get("dual_fisheye")
        return cal

    def source_graph(self, mount: Mount, level: tuple[float, float] | None = None, width: int | None = None) -> str:
        """Filter graph from the input to an equirectangular picture in the
        aircraft (or holder) frame, labelled [eq]. level = (pitch, roll) replaces
        the mount's fixed tilt, for a camera that is levelled clip by clip.
        width: a smaller sphere than the source gives (for stills), scaled before the stitch."""
        cal = self.calibration(mount)
        yaw, pitch, roll = (cal.yaw, cal.pitch, cal.roll) if cal else (0.0, 0.0, 0.0)
        if level is not None and not (cal and cal.stabilized):
            pitch, roll = level[0], level[1]
            if len(level) > 2:                      # the nose was found from the sensors too (core/mountcal.py)
                yaw = (NOSE_SIGN * level[2] + NOSE_OFFSET + 180) % 360 - 180
        else:
            level = None
        rot = f":yaw={yaw:g}:pitch={pitch:g}:roll={roll:g}"
        if level is not None or (cal and cal.rorder != "ypr"):
            rot += f":rorder={'rpy' if level is not None else cal.rorder}"
        if self.kind in ("dual_fisheye", "insv"):
            fov = cal.lens_fov if cal and cal.lens_fov else 190
            if self.kind == "insv":          # one video track per lens
                w = min(self.size[0] * 2, cal.width) if cal and cal.width else self.size[0] * 2
                w = min(w, width) if width else w
                lens = f"scale={w // 2}:{w // 2}" if w < self.size[0] * 2 else "null"
                head = f"[0:v:1]{lens}[l1];[0:v:0]{lens}[l0];[l1][l0]hstack=inputs=2,"
            else:
                w = min(self.size[0], width) if width else self.size[0]
                head = f"[0:v:0]scale={w}:{w // 2}," if w < self.size[0] else "[0:v:0]"
            return f"{head}v360=input=dfisheye:output=e:ih_fov={fov:g}:iv_fov={fov:g}:w={w}:h={w // 2}{rot}{EQ}"
        if yaw or pitch or roll:
            return f"[0:v:0]v360=input=e:output=e{rot}{EQ}"
        return f"[0:v:0]null{EQ}"


# Decoding for a single frame: on the graphics card when there is one, keyframes only.
STILL_FLAGS = ["-hwaccel", "auto", "-skip_frame", "nokey"]

# v360 yaw that brings a nose found by core.mountcal at +x degrees to the middle of the picture.
# Both numbers were found on a real flight with the camera in two different places.
NOSE_SIGN = -1.0
NOSE_OFFSET = 180.0


def discover(folder: Path) -> tuple[list[Recording], list[Path]]:
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir() if p.is_file())
    igc = [p for p in files if p.suffix.lower() == ".igc"]

    groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for p in files:
        m = _CAMERA.match(p.name)
        if m:
            kind = "dual_fisheye" if m[4].lower() == "lrv" else "insv"
            groups.setdefault((m[2], kind), []).append((int(m[3]), p))
        elif p.suffix.lower() == ".mp4":
            groups.setdefault((p.stem, "equirect"), []).append((0, p))

    takes: dict[str, dict[str, Recording]] = {}
    for (rec_id, kind), parts in sorted(groups.items()):
        paths = [p for _, p in sorted(parts)]
        try:
            infos = [ffmpeg.probe(p) for p in paths]
        except RuntimeError:            # a file that is still being copied
            continue
        if not infos[0].video_sizes:
            continue
        takes.setdefault(rec_id, {})[kind] = Recording(
            rec_id, kind, paths, [i.duration_s for i in infos], infos[0].creation_utc, infos[0].video_sizes[0])

    recordings = []
    for rec_id, kinds in sorted(takes.items()):
        # The small proxy is the working copy when the camera made one.
        main = kinds.get("dual_fisheye") or kinds.get("equirect") or kinds["insv"]
        main.alt = {k: r for k, r in kinds.items() if r is not main}
        recordings.append(main)
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

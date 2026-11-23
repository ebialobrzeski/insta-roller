"""Render a framing plan with FFmpeg: equirectangular -> flat frame, then join.

The virtual camera is animated with sendcmd expressions on the v360 filter.
reset_rot=1 makes each angle command absolute instead of cumulative.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import ffmpeg
from .config import Mount, Preset
from .contracts import FramedShot, FramingPlan, Keyframe
from .ingest import Recording, fingerprint

FLAT = "v360@flat"


def _wrap(yaw: float) -> float:
    return (yaw + 180) % 360 - 180


def _wrap_expr(expr: str) -> str:
    return f"mod(mod(({expr})+180,360)+360,360)-180"


def sendcmd_script(keyframes: list[Keyframe]) -> str:
    """Commands that move the camera between keyframes with a smoothstep ease."""
    ease = "(TI*TI*(3-2*TI))"
    lines = []
    for a, b in zip(keyframes, keyframes[1:]):
        if b.t - a.t < 1e-3:
            continue
        cmds = []
        for name, option, wrap in (("yaw", "yaw", True), ("pitch", "pitch", False),
                                   ("roll", "roll", False), ("fov", "d_fov", False)):
            va, vb = getattr(a, name), getattr(b, name)
            if abs(vb - va) < 1e-6:
                value = _wrap(va) if wrap else va
                cmds.append(f"[enter] {FLAT} {option} {value:.4f}")
            else:
                expr = f"{va:.4f}+({vb - va:.4f})*{ease}"
                cmds.append(f"[expr] {FLAT} {option} '{_wrap_expr(expr) if wrap else expr}'")
        lines.append(f"{a.t:.3f}-{b.t:.3f} " + ",\n    ".join(cmds) + ";")
    return "\n".join(lines) + "\n"


def flat_filter(first: Keyframe | Preset, width: int, height: int) -> str:
    return (f"{FLAT}=input=e:output=flat:w={width}:h={height}:d_fov={first.fov}"
            f":yaw={_wrap(first.yaw):.4f}:pitch={first.pitch}:roll={first.roll}:reset_rot=1:interp=cubic")


def encoder_args(encoder: str) -> list[str]:
    if encoder == "auto":
        encoder = "h264_nvenc" if ffmpeg.has_encoder("h264_nvenc") else "libx264"
    if encoder.endswith("_nvenc"):
        return ["-c:v", encoder, "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    return ["-c:v", encoder, "-preset", "medium", "-crf", "19"]


def render_shot(shot: FramedShot, plan: FramingPlan, rec: Recording, mount: Mount,
                encoder: str, work: Path, log: Path) -> Path:
    shots_dir = work / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    source = rec.to_aircraft_frame(mount)
    key = fingerprint(shot.model_dump_json(exclude={"index"}), plan.width, plan.height, plan.fps,
                      encoder, source, rec.id, *rec.files)
    out = shots_dir / f"{key}.mp4"
    if out.exists():
        return out

    script = shots_dir / f"{key}.cmd"
    script.write_text(sendcmd_script(shot.keyframes), encoding="ascii")
    chain = [source] if source else []
    chain += [f"sendcmd=f={script.name}", flat_filter(shot.keyframes[0], plan.width, plan.height),
              "format=yuv420p"]
    tmp = shots_dir / f"{key}.part.mp4"
    ffmpeg.run(["-t", f"{shot.duration_s:.3f}", *rec.input_args(work, shot.start_s, shot.duration_s),
                "-vf", ",".join(chain), "-r", f"{plan.fps:g}", "-an",
                *encoder_args(encoder), tmp.name], log=log, cwd=shots_dir)
    tmp.replace(out)
    return out


def render_reel(plan: FramingPlan, rec: Recording, mount: Mount, encoder: str,
                work: Path, out: Path, log: Path, progress=print) -> Path:
    parts = []
    for shot in plan.shots:
        progress(f"  shot {shot.index}/{len(plan.shots)}  {shot.scene_id}  {shot.duration_s:.1f}s")
        parts.append(render_shot(shot, plan, rec, mount, encoder, work, log))
    listing = work / f"join_{out.stem}.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-movflags", "+faststart", str(out)], log=log)
    return out


def preview_presets(rec: Recording, mount: Mount, at_s: float, width: int, height: int,
                    work: Path, out_dir: Path, log: Path) -> Path:
    """One still per preset plus a contact sheet, for checking the calibration."""
    out_dir.mkdir(parents=True, exist_ok=True)
    source = rec.to_aircraft_frame(mount)
    stills = []
    for name, preset in mount.presets.items():
        still = out_dir / f"preset_{name}.jpg"
        chain = ([source] if source else []) + [
            flat_filter(preset, width, height),
            f"drawtext=text='{name}':x=20:y=20:fontsize=40:fontcolor=white:box=1:boxcolor=black@0.5"]
        try:
            ffmpeg.run([*rec.input_args(work, at_s), "-frames:v", "1",
                        "-vf", ",".join(chain), "-q:v", "3", str(still)], log=log)
        except RuntimeError:        # no font available for the label
            ffmpeg.run([*rec.input_args(work, at_s), "-frames:v", "1",
                        "-vf", ",".join(chain[:-1]), "-q:v", "3", str(still)], log=log)
        stills.append(still)
    sheet = out_dir / "presets.jpg"
    args = [a for s in stills for a in ("-i", str(s))]
    ffmpeg.run([*args, "-filter_complex", f"hstack=inputs={len(stills)}", "-q:v", "3", str(sheet)], log=log)
    return sheet


def clear_cache(work: Path) -> None:
    shutil.rmtree(work / "shots", ignore_errors=True)

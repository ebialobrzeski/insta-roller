"""Render a framing plan with FFmpeg: equirectangular -> flat frame, then join.

The virtual camera is animated with sendcmd expressions on the v360 filter.
reset_rot=1 makes each angle command absolute instead of cumulative.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg, overlay
from .config import Mount, Preset, Profile, SafeZone
from .contracts import FramedShot, FramingPlan, Keyframe, Telemetry
from .ingest import EQ, Recording, fingerprint

FLAT = "v360@flat"
EV = "exposure@ev"


def _wrap(yaw: float) -> float:
    return (yaw + 180) % 360 - 180


def _wrap_expr(expr: str) -> str:
    return f"mod(mod(({expr})+180,360)+360,360)-180"


def sendcmd_script(keyframes: list[Keyframe], with_ev: bool = False) -> str:
    """Commands that move the camera between keyframes with a smoothstep ease.
    Dense keyframes (a levelled horizon) are joined linearly instead."""
    dense = len(keyframes) > 2 and max(b.t - a.t for a, b in zip(keyframes, keyframes[1:])) <= 1.0
    ease = "TI" if dense else "(TI*TI*(3-2*TI))"
    params = [("yaw", FLAT, "yaw", True), ("pitch", FLAT, "pitch", False),
              ("roll", FLAT, "roll", True), ("fov", FLAT, "d_fov", False)]
    if with_ev:
        params.append(("ev", EV, "exposure", False))
    lines = []
    for a, b in zip(keyframes, keyframes[1:]):
        if b.t - a.t < 1e-3:
            continue
        cmds = []
        for name, target, option, wrap in params:
            va, vb = getattr(a, name), getattr(b, name)
            if abs(vb - va) < 1e-6:
                value = _wrap(va) if wrap else va
                cmds.append(f"[enter] {target} {option} {value:.4f}")
            else:
                expr = f"{va:.4f}+({vb - va:.4f})*{ease}"
                cmds.append(f"[expr] {target} {option} '{_wrap_expr(expr) if wrap else expr}'")
        lines.append(f"{a.t:.3f}-{b.t:.3f} " + ",\n    ".join(cmds) + ";")
    return "\n".join(lines) + "\n"


OUTPUT = {"flat": "flat", "planet": "sg", "fisheye": "fisheye", "pano": "e"}     # v360 names; pano = the whole sphere


def flat_filter(first: Keyframe | Preset, width: int, height: int, projection: str = "flat") -> str:
    projection = getattr(first, "projection", projection)
    return (f"{FLAT}=input=e:output={OUTPUT[projection]}:w={width}:h={height}:d_fov={first.fov:g}"
            f":yaw={_wrap(first.yaw):.4f}:pitch={first.pitch:g}:roll={_wrap(first.roll):g}:reset_rot=1:interp=cubic")


def encoder_args(encoder: str) -> list[str]:
    if encoder == "auto":
        encoder = "h264_nvenc" if ffmpeg.has_encoder("h264_nvenc") else "libx264"
    if encoder.endswith("_nvenc"):
        return ["-c:v", encoder, "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    return ["-c:v", encoder, "-preset", "medium", "-crf", "19"]


@dataclass
class Context:
    """Everything a render needs besides the plan."""
    recordings: dict[str, Recording]      # by id; "" = the job's main recording
    mount: Mount
    profile: Profile
    work: Path
    log: Path
    telemetry: Telemetry | None = None
    levels: dict[str, tuple[float, float]] | None = None    # scene id -> (pitch, roll)
    stitcher: object | None = None                          # core.stitch.Stitcher
    source: str = "auto"                                    # auto = sharpest files | proxy

    def recording(self, rec_id: str) -> Recording:
        return self.recordings.get(rec_id) or self.recordings[""]


def _with_handles(shot: FramedShot, rec: Recording, handles_s: float) -> FramedShot:
    """The shot widened by spare footage at both ends; the camera holds still there."""
    if handles_s <= 0:
        return shot
    pre = min(handles_s * shot.speed, shot.start_s)
    post = max(min(handles_s * shot.speed, rec.duration_s - shot.start_s - shot.duration_s), 0.0)
    frames = [k.model_copy(update={"t": round(k.t + pre, 3)}) for k in shot.keyframes]
    frames = [frames[0].model_copy(update={"t": 0.0})] + frames + \
             [frames[-1].model_copy(update={"t": round(shot.duration_s + pre + post, 3)})]
    return shot.model_copy(update={"start_s": round(shot.start_s - pre, 3),
                                   "duration_s": round(shot.duration_s + pre + post, 3), "keyframes": frames})


def render_shot(shot: FramedShot, plan: FramingPlan, ctx: Context, handles_s: float = 0.0) -> Path:
    profile, work = ctx.profile, ctx.work
    shots_dir = work / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    rec = ctx.recording(shot.recording)
    shot = _with_handles(shot, rec, handles_s)
    level = (ctx.levels or {}).get(shot.scene_id)

    start = shot.start_s
    if ctx.stitcher is not None and ctx.source != "proxy" and "insv" in (rec.kind, *rec.alt):      # full-quality stitch of this span only
        cal = ctx.mount.calibration.get("sdk")
        rec = ctx.stitcher.segment(rec.variant("insv"), shot.start_s, shot.duration_s, ctx.log,
                                   cal.stabilize if cal else "none", cal.width if cal else None)
        start = 0.0
    else:
        rec = rec if ctx.source == "proxy" else rec.best()
    source = rec.source_graph(ctx.mount, level)

    with_ev = any(abs(k.ev) > 1e-6 for k in shot.keyframes)
    out_s = shot.duration_s / shot.speed
    frames = b""
    if ctx.telemetry is not None and profile.overlay.enabled and shot.speed == 1.0:
        frames = overlay.frames_rgba(profile.overlay, ctx.telemetry, plan.width, shot.start_s, shot.duration_s)
    audio = profile.audio
    live_audio = audio and shot.speed == 1.0 and ffmpeg.probe(rec.files[0]).audio
    key = fingerprint(shot.model_dump_json(exclude={"index"}), plan.width, plan.height, plan.fps,
                      profile.encoder, source, rec.id, *rec.files, with_ev, audio, profile.audio_fade_s,
                      fingerprint(frames) if frames else "", profile.safe_zone.model_dump_json() if frames else "")
    out = shots_dir / f"{key}.mp4"
    if out.exists():
        out.touch()
        return out

    script = shots_dir / f"{key}.cmd"
    script.write_text(sendcmd_script(shot.keyframes, with_ev), encoding="ascii")
    chain = [f"sendcmd=f={script.name}", flat_filter(shot.keyframes[0], plan.width, plan.height, shot.projection)]
    if with_ev:
        chain.append(f"{EV}=exposure={shot.keyframes[0].ev:.3f}")
    chain.append("format=yuv420p")
    if shot.speed != 1.0:
        chain.append(f"setpts=PTS/{shot.speed:g}")
    graph = f"{source};{EQ}{','.join(chain)}[v0]"
    inputs = ["-t", f"{shot.duration_s:.3f}", *rec.input_args(work, start, shot.duration_s)]
    if frames:
        w, h = overlay.canvas_size(profile.overlay, plan.width)
        x, y = overlay.position(profile.overlay, (plan.width, plan.height), profile.safe_zone)
        inputs += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", f"{profile.overlay.fps:g}", "-i", "pipe:0"]
        graph += f";[1:v]format=rgba[ov];[v0][ov]overlay=x={x}:y={y}:eof_action=repeat[v]"
    else:
        graph += ";[v0]null[v]"
    maps = ["-map", "[v]"]
    if audio:
        fade = min(profile.audio_fade_s, out_s / 3)
        if live_audio:
            graph += (f";[0:a:0]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d={fade:.3f},"
                      f"afade=t=out:st={out_s - fade:.3f}:d={fade:.3f}[a]")
        else:                               # sped-up or silent source: keep the track, but quiet
            graph += f";anullsrc=r=48000:cl=stereo,atrim=duration={out_s:.3f}[a]"
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    tmp = shots_dir / f"{key}.part.mp4"
    ffmpeg.run([*inputs, "-filter_complex", graph, *maps, "-r", f"{plan.fps:g}", "-t", f"{out_s:.3f}",
                *encoder_args(profile.encoder), tmp.name],
               log=ctx.log, cwd=shots_dir, stdin=frames or None)
    tmp.replace(out)
    return out


def render_reel(plan: FramingPlan, ctx: Context, out: Path, progress=print) -> Path:
    parts = []
    for shot in plan.shots:
        progress(f"  shot {shot.index}/{len(plan.shots)}  {shot.scene_id}  {shot.duration_s / shot.speed:.1f}s")
        parts.append(render_shot(shot, plan, ctx))
    listing = ctx.work / f"join_{out.stem}.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-movflags", "+faststart", str(out)], log=ctx.log)
    return out


def export_shots(plan: FramingPlan, ctx: Context, out_dir: Path, progress=print) -> list[Path]:
    """Every shot of the reel as its own H.264 file, numbered in reel order, with
    spare footage at both ends for trimming in a phone app."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.mp4"):
        old.unlink()
    files = []
    for shot in plan.shots:
        progress(f"  shot {shot.index}/{len(plan.shots)}  {shot.scene_id}")
        part = render_shot(shot, plan, ctx, handles_s=ctx.profile.shot_handles_s)
        name = shot.scene_id.split("_", 1)[-1] if shot.type in ("", "ending") else shot.type
        target = out_dir / f"{shot.index:02d}_{'ending' if shot.type == 'ending' else name}.mp4"
        shutil.copyfile(part, target)
        files.append(target)
    return files


def zone_boxes(zone: SafeZone, width: int, height: int) -> list[str]:
    """drawbox filters that shade what the platform interface covers."""
    boxes = []
    style = "color=red@0.25:t=fill"
    if zone.top:
        boxes.append(f"drawbox=x=0:y=0:w={width}:h={int(height * zone.top)}:{style}")
    if zone.bottom:
        boxes.append(f"drawbox=x=0:y={int(height * (1 - zone.bottom))}:w={width}:h={int(height * zone.bottom)}:{style}")
    if zone.left:
        boxes.append(f"drawbox=x=0:y=0:w={int(width * zone.left)}:h={height}:{style}")
    if zone.right:
        boxes.append(f"drawbox=x={int(width * (1 - zone.right))}:y=0:w={int(width * zone.right)}:h={height}:{style}")
    return boxes


def still(rec: Recording, mount: Mount, at_s: float, view: Keyframe | Preset, size: tuple[int, int],
          out: Path, work: Path, log: Path, zone: SafeZone | None = None, label: str = "",
          level: tuple[float, float] | None = None, projection: str = "flat") -> Path:
    """One flat frame of the recording, for previews and scene thumbnails."""
    out.parent.mkdir(parents=True, exist_ok=True)
    chain = [flat_filter(view, *size, projection)] + (zone_boxes(zone, *size) if zone else [])
    labelled = chain + [f"drawtext=text='{label}':x=20:y=20:fontsize={size[1] // 24}:fontcolor=white"
                        ":box=1:boxcolor=black@0.5"]
    for attempt in ([labelled, chain] if label else [chain]):
        try:
            ffmpeg.run([*rec.input_args(work, at_s), "-frames:v", "1", "-filter_complex",
                        f"{rec.source_graph(mount, level)};{EQ}{','.join(attempt)}[v]", "-map", "[v]",
                        "-q:v", "3", str(out)], log=log)
            return out
        except RuntimeError:                # no font available for the label
            if attempt is chain:
                raise
    return out


def preview_presets(rec: Recording, mount: Mount, at_s: float, size: tuple[int, int], work: Path,
                    out_dir: Path, log: Path, zone: SafeZone | None = None,
                    level: tuple[float, float] | None = None) -> Path:
    """One still per preset plus a contact sheet, for checking the calibration.
    Presets are shown as the reels frame them: shifted into the free area."""
    from .framing import frame_shot
    stills = []
    for name in mount.presets:
        view = frame_shot([name], 1.0, mount, 0.0, zone, size)[0]
        stills.append(still(rec, mount, at_s, view, size, out_dir / f"preset_{name}.jpg", work, log, zone, name, level,
                            mount.presets[name].projection))
    sheet = out_dir / "presets.jpg"
    args = [a for s in stills for a in ("-i", str(s))]
    ffmpeg.run([*args, "-filter_complex", f"hstack=inputs={len(stills)}", "-q:v", "3", str(sheet)], log=log)
    return sheet


def clear_cache(work: Path) -> None:
    shutil.rmtree(work / "shots", ignore_errors=True)

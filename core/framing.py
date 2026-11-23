"""Framing plan: virtual camera keyframes (time -> yaw / pitch / roll / fov) per shot."""
from __future__ import annotations

import math

from .config import Calibration, Mount, Profile, SafeZone
from .contracts import FramedShot, FramingPlan, Keyframe, ReelPlan, Shot, Telemetry
from .ingest import fingerprint

# Sign conventions of the v360 flat view, checked on real footage.
PITCH_UP = 1.0          # positive pitch looks up
ROLL_FOR_BANK = -1.0    # view roll that levels a right bank when looking ahead
PITCH_FOR_BANK = 1.0
LEVEL_STEP_S = 0.5


def _shortest(prev_yaw: float, target: float) -> float:
    """Target yaw expressed so that the move from prev_yaw takes the short way."""
    return prev_yaw + ((target - prev_yaw + 180) % 360 - 180)


def safe_shift(fov: float, width: int, height: int, zone: SafeZone) -> tuple[float, float]:
    """(yaw, pitch) change that moves the subject from the frame centre to the
    centre of the area the platform interface leaves free."""
    diag = math.hypot(width, height)
    tan_d = math.tan(math.radians(min(fov, 175.0)) / 2)
    cx = (zone.left - zone.right) / 2          # > 0: the free area sits right of centre
    cy = (zone.bottom - zone.top) / 2          # > 0: the free area sits above centre
    dyaw = -math.degrees(math.atan(2 * cx * tan_d * width / diag))
    dpitch = -math.degrees(math.atan(2 * cy * tan_d * height / diag)) * PITCH_UP
    return dyaw, dpitch


def frame_shot(presets: list[str], duration_s: float, mount: Mount, transition_s: float,
               zone: SafeZone | None = None, size: tuple[int, int] = (1080, 1920),
               attrs: dict | None = None, start_s: float = 0.0) -> list[Keyframe]:
    """start_s: video time of the shot start, to follow a target that moves (attrs["track_<name>"])."""
    n = len(presets)
    hold = duration_s / n
    move = min(transition_s, hold * 0.6)
    frames: list[Keyframe] = []
    yaw = None
    for i, name in enumerate(presets):
        p = mount.presets[name]
        target, pitch = p.yaw, p.pitch
        if p.track:                                 # follow a direction found by the analysis
            target += float((attrs or {}).get(f"yaw_{p.track}", 0.0))
        if zone and p.safe_zone:
            dyaw, dpitch = safe_shift(p.fov, size[0], size[1], zone)
            target, pitch = target + dyaw, pitch + dpitch
        yaw = target if yaw is None else _shortest(yaw, target)
        t_in = 0.0 if i == 0 else i * hold + move / 2
        t_out = duration_s if i == n - 1 else (i + 1) * hold - move / 2
        frames.append(Keyframe(t=round(t_in, 3), yaw=yaw, pitch=pitch, roll=p.roll, fov=p.fov, ev=p.exposure_ev))
        track = (attrs or {}).get(f"track_{p.track}") if p.track else None
        if track and len(track) > 1:                # the target moves: follow it through the hold
            offset = target - float((attrs or {}).get(f"yaw_{p.track}", 0.0))
            inside = [t - start_s for t, _ in track if t_in < t - start_s < t_out]
            for t in [t_in, *inside, t_out]:
                yaw = _shortest(yaw, _track_yaw(track, start_s + t) + offset)
                if t == t_in:
                    frames[-1].yaw = yaw
                else:
                    frames.append(Keyframe(t=round(t, 3), yaw=yaw, pitch=pitch, roll=p.roll, fov=p.fov,
                                           ev=p.exposure_ev))
            continue
        yaw += p.pan_dps * (t_out - t_in)
        frames.append(Keyframe(t=round(t_out, 3), yaw=yaw, pitch=pitch, roll=p.roll, fov=p.fov, ev=p.exposure_ev))
    return frames


def _track_yaw(track: list, t: float) -> float:
    """Yaw of a tracked target at video time t; track = [[time, yaw], ...] in time order."""
    if t <= track[0][0]:
        return float(track[0][1])
    for (t0, y0), (t1, y1) in zip(track, track[1:]):
        if t <= t1:
            return y0 + ((y1 - y0 + 180) % 360 - 180) * (t - t0) / max(t1 - t0, 1e-6)
    return float(track[-1][1])


def _ease(a: float, b: float, k: float) -> float:
    return a + (b - a) * k * k * (3 - 2 * k)


def _at(frames: list[Keyframe], t: float) -> Keyframe:
    """The smoothstep interpolation the renderer applies, evaluated at t."""
    for a, b in zip(frames, frames[1:]):
        if t <= b.t:
            k = 0.0 if b.t - a.t < 1e-6 else min(max((t - a.t) / (b.t - a.t), 0.0), 1.0)
            return Keyframe(t=t, yaw=_ease(a.yaw, b.yaw, k), pitch=_ease(a.pitch, b.pitch, k),
                            roll=_ease(a.roll, b.roll, k), fov=_ease(a.fov, b.fov, k), ev=_ease(a.ev, b.ev, k))
    return frames[-1].model_copy(update={"t": t})


def level_horizon(frames: list[Keyframe], locked: list[float], shot: Shot, telemetry: Telemetry,
                  stabilized: bool) -> list[Keyframe]:
    """Dense keyframes that hold the horizon level, or tie it to the aircraft on a
    stabilised source, using the bank angle. locked[i] says how much the preset of
    keyframe i wants a level horizon (1) or the aircraft's (0)."""
    bank_at = {round(s.t): s.bank_deg for s in telemetry.samples}
    weights = [Keyframe(t=f.t, yaw=w, pitch=0, roll=0, fov=1) for f, w in zip(frames, locked)]
    out, t = [], 0.0
    while True:
        t = min(t, shot.duration_s)
        base = _at(frames, t)
        lock = _at(weights, t).yaw
        video_t = shot.start_s + t
        lo = math.floor(video_t)
        b0 = bank_at.get(lo, 0.0)
        bank = b0 + (bank_at.get(lo + 1, b0) - b0) * (video_t - lo)
        amount = -(1.0 - lock) if stabilized else lock      # a stabilised source needs the bank put back
        psi = math.radians(base.yaw)
        base.roll += ROLL_FOR_BANK * amount * bank * math.cos(psi)
        base.pitch += PITCH_FOR_BANK * amount * bank * math.sin(psi)
        base.t = round(t, 3)
        out.append(base)
        if t >= shot.duration_s:
            return out
        t += LEVEL_STEP_S


def plan_framing(reel: ReelPlan, profile: Profile, mount: Mount, telemetry: Telemetry | None = None,
                 calibration: Calibration | None = None) -> FramingPlan:
    """`source` identifies everything the plan was made from; `plan` keeps a
    hand-edited framing plan only while that stays the same."""
    out = profile.output
    stabilized = bool(calibration and calibration.stabilized)
    shots = []
    for s in reel.shots:
        frames = frame_shot(s.framing, s.duration_s, mount, profile.transition_s * s.speed, profile.safe_zone,
                            (out.width, out.height), s.attrs, s.start_s)
        locked = [1.0 if mount.presets[name].horizon == "locked" else 0.0 for name in s.framing for _ in (0, 1)]
        needs = any(w == (0.0 if stabilized else 1.0) for w in locked)
        if telemetry and telemetry.samples and needs:
            frames = level_horizon(frames, locked, s, telemetry, stabilized)
        shots.append(FramedShot(index=s.index, scene_id=s.scene_id, start_s=s.start_s, duration_s=s.duration_s,
                                keyframes=frames, recording=s.recording, speed=s.speed, type=s.type))
    return FramingPlan(theme=reel.theme, length_s=reel.length_s, width=out.width, height=out.height,
                       fps=out.fps, shots=shots,
                       source=fingerprint(reel.model_dump_json(), mount.model_dump_json(), out.model_dump_json(),
                                          profile.safe_zone.model_dump_json(), profile.transition_s, stabilized,
                                          telemetry.model_dump_json() if telemetry else "",
                                          ROLL_FOR_BANK, PITCH_FOR_BANK, PITCH_UP))

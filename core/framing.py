"""Framing plan: virtual camera keyframes (time -> yaw / pitch / roll / fov) per shot."""
from __future__ import annotations

from .config import Mount, Profile
from .contracts import FramedShot, FramingPlan, Keyframe, ReelPlan


def _shortest(prev_yaw: float, target: float) -> float:
    """Target yaw expressed so that the move from prev_yaw takes the short way."""
    return prev_yaw + ((target - prev_yaw + 180) % 360 - 180)


def frame_shot(presets: list[str], duration_s: float, mount: Mount, transition_s: float) -> list[Keyframe]:
    n = len(presets)
    hold = duration_s / n
    move = min(transition_s, hold * 0.6)
    frames: list[Keyframe] = []
    yaw = mount.presets[presets[0]].yaw
    for i, name in enumerate(presets):
        p = mount.presets[name]
        yaw = _shortest(yaw, p.yaw)
        t_in = 0.0 if i == 0 else i * hold + move / 2
        t_out = duration_s if i == n - 1 else (i + 1) * hold - move / 2
        for t in (t_in, t_out):
            frames.append(Keyframe(t=round(t, 3), yaw=yaw, pitch=p.pitch, roll=p.roll, fov=p.fov))
    return frames


def plan_framing(reel: ReelPlan, profile: Profile, mount: Mount) -> FramingPlan:
    shots = [FramedShot(index=s.index, scene_id=s.scene_id, start_s=s.start_s, duration_s=s.duration_s,
                        keyframes=frame_shot(s.framing, s.duration_s, mount, profile.transition_s))
             for s in reel.shots]
    out = profile.output
    return FramingPlan(theme=reel.theme, length_s=reel.length_s, width=out.width, height=out.height,
                       fps=out.fps, shots=shots)

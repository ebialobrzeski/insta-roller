"""Framing plan: virtual camera keyframes (time -> yaw / pitch / roll / fov) per shot."""
from __future__ import annotations

import math

import numpy as np

from .config import Calibration, Mount, Preset, Profile, SafeZone
from .contracts import FramedShot, FramingPlan, Keyframe, ReelPlan, Shot, Telemetry
from .ingest import fingerprint

# Sign conventions of the v360 flat view, checked on real footage.
PITCH_UP = 1.0          # positive pitch looks up
ROLL_FOR_BANK = -1.0    # view roll that levels a right bank when looking ahead
PITCH_FOR_BANK = 1.0
LEVEL_STEP_S = 0.5
FOLLOW_STEP_S = 0.5     # keyframes of a view that follows its subject
HOME_NEAR_M = 400.0     # closer to where the flight began than this, a view cannot be aimed at it


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
               attrs: dict | None = None, start_s: float = 0.0,
               max_turn_dps: float = 0.0, max_track_dps: float = 0.0) -> list[Keyframe]:
    """start_s: video time of the shot start, to follow a target that moves (attrs["track_<name>"]).
    max_turn_dps stretches a move between presets so it is never faster than that;
    max_track_dps lets the camera lag behind a target rather than whip after it."""
    attrs = attrs or {}
    n = len(presets)
    hold = duration_s / n

    def aim(name: str) -> tuple[float, float]:
        p = mount.presets[name]
        yaw, pitch = p.yaw, p.pitch
        if p.track:                                 # a direction (and height) found by the analysis
            yaw += float(attrs.get(f"yaw_{p.track}", 0.0))
            pitch += float(attrs.get(f"pitch_{p.track}", 0.0))
        if zone and p.safe_zone:
            dyaw, dpitch = safe_shift(p.fov, size[0], size[1], zone)
            yaw, pitch = yaw + dyaw, pitch + dpitch
        return yaw, pitch

    aims = [aim(name) for name in presets]
    move = min(transition_s, hold * 0.6)
    if max_turn_dps > 0 and n > 1:                  # a long way round takes longer
        widest = max(max(abs((b[0] - a[0] + 180) % 360 - 180), abs(b[1] - a[1])) for a, b in zip(aims, aims[1:]))
        move = min(max(move, widest / max_turn_dps), hold * 0.85)

    frames: list[Keyframe] = []
    yaw = None
    for i, name in enumerate(presets):
        p = mount.presets[name]
        target, pitch = aims[i]
        yaw = target if yaw is None else _shortest(yaw, target)
        t_in = 0.0 if i == 0 else i * hold + move / 2
        t_out = duration_s if i == n - 1 else (i + 1) * hold - move / 2
        frames.append(Keyframe(t=round(t_in, 3), yaw=yaw, pitch=pitch, roll=p.roll, fov=p.fov, ev=p.exposure_ev))
        track = attrs.get(f"track_{p.track}") if p.track else None
        if track and len(track) > 1:                # the target moves: follow it through the hold
            dyaw = target - float(attrs.get(f"yaw_{p.track}", 0.0))
            dpitch = pitch - float(attrs.get(f"pitch_{p.track}", 0.0))
            inside = [row[0] - start_s for row in track if t_in < row[0] - start_s < t_out]
            last_t = t_in
            for t in [t_in, *inside, t_out]:
                want = _shortest(yaw, _track(track, start_s + t, 1) + dyaw)
                if t > t_in and max_track_dps > 0:
                    limit = max_track_dps * (t - last_t)
                    want = yaw + min(max(want - yaw, -limit), limit)
                yaw, last_t = want, t
                at_pitch = _track(track, start_s + t, 2) + dpitch if len(track[0]) > 2 else pitch
                if t == t_in:
                    frames[-1].yaw, frames[-1].pitch = yaw, at_pitch
                else:
                    frames.append(Keyframe(t=round(t, 3), yaw=yaw, pitch=at_pitch, roll=p.roll, fov=p.fov,
                                           ev=p.exposure_ev))
            continue
        if p.path:                                  # the subject is somewhere else later in the shot: go with it
            shift_yaw, shift_pitch = yaw - p.yaw, pitch - p.pitch
            points = [(t_in, yaw, pitch)]
            for part, at_yaw, at_pitch in sorted(p.path):
                t = min(max(part * duration_s, t_in), t_out)
                if t > points[-1][0] + 0.2:
                    want = _shortest(points[-1][1], at_yaw + shift_yaw)
                    if max_turn_dps > 0:            # a subject that jumps is followed at the speed a camera may turn
                        limit = max_turn_dps * (t - points[-1][0])
                        want = points[-1][1] + min(max(want - points[-1][1], -limit), limit)
                    points.append((t, want, at_pitch + shift_pitch))
            if points[-1][0] < t_out - 0.2:
                points.append((t_out, points[-1][1], points[-1][2]))
            t = t_in + FOLLOW_STEP_S
            while t < t_out - 1e-6:                 # dense, so the move does not stop at every point
                for (ta, ya, pa), (tb, yb, pb) in zip(points, points[1:]):
                    if ta <= t <= tb:
                        k = (t - ta) / max(tb - ta, 1e-6)
                        frames.append(Keyframe(t=round(t, 3), yaw=ya + (yb - ya) * k, pitch=pa + (pb - pa) * k, roll=p.roll,
                                               fov=p.fov if p.fov_end is None else p.fov + (p.fov_end - p.fov) * (t - t_in) / (t_out - t_in),
                                               ev=p.exposure_ev))
                        break
                t += FOLLOW_STEP_S
            yaw = points[-1][1]
            frames.append(Keyframe(t=round(t_out, 3), yaw=yaw, pitch=points[-1][2], roll=p.roll,
                                   fov=p.fov if p.fov_end is None else p.fov_end, ev=p.exposure_ev))
            continue
        yaw += p.pan_dps * (t_out - t_in)
        frames.append(Keyframe(t=round(t_out, 3), yaw=yaw, pitch=pitch, roll=p.roll + 360.0 * p.roll_turns,
                               fov=p.fov if p.fov_end is None else p.fov_end, ev=p.exposure_ev))
    return frames


def _track(track: list, t: float, column: int) -> float:
    """A tracked target at video time t; track = [[time, yaw, pitch], ...] in time order."""
    if t <= track[0][0]:
        return float(track[0][column])
    for a, b in zip(track, track[1:]):
        if t <= b[0]:
            step = b[column] - a[column]
            if column == 1:
                step = (step + 180) % 360 - 180         # yaw takes the short way
            return a[column] + step * (t - a[0]) / max(b[0] - a[0], 1e-6)
    return float(track[-1][column])


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


class Motion:
    """What the aircraft does, at any video time: bank, heading, position."""

    def __init__(self, telemetry: Telemetry | None):
        samples = sorted(telemetry.samples, key=lambda x: x.t) if telemetry else []
        self.t = np.array([x.t for x in samples])
        self.bank = np.array([x.bank_deg for x in samples])
        known = bool(samples) and all(x.heading_deg is not None and x.lat is not None for x in samples)
        self.heading = np.degrees(np.unwrap(np.radians([x.heading_deg for x in samples]))) if known else None
        self.lat = np.array([x.lat for x in samples]) if known else None
        self.lon = np.array([x.lon for x in samples]) if known else None
        self.alt = np.array([x.alt_m for x in samples])
        self.home = tuple(telemetry.home) if telemetry and telemetry.home else None

    def __bool__(self) -> bool:
        return len(self.t) > 0

    @property
    def world(self) -> bool:
        return self.heading is not None

    def bank_at(self, t: float) -> float:
        return float(np.interp(t, self.t, self.bank)) if len(self.t) else 0.0

    def heading_at(self, t: float) -> float:
        return float(np.interp(t, self.t, self.heading))

    def home_at(self, t: float) -> tuple[float, float] | None:
        """(yaw from the nose, pitch) at which the place the flight began is seen."""
        if not self.world or self.home is None:
            return None
        from .heading import bearing_down
        bearing, down, distance = bearing_down(float(np.interp(t, self.t, self.lat)), float(np.interp(t, self.t, self.lon)),
                                               float(np.interp(t, self.t, self.alt)), self.home)
        if distance < HOME_NEAR_M:                  # still on it (takeoff, landing): there is no direction to it
            return None
        return bearing - self.heading_at(t), -down * PITCH_UP


def animate(frames: list[Keyframe], presets: list[Preset], shot: Shot, motion: Motion, stabilized: bool) -> list[Keyframe]:
    """Dense keyframes for the things that follow the aircraft's motion through a shot:
    a level horizon (or, on a stabilised source, the aircraft's own), a view held on a
    compass direction, a view held on the place the flight began.
    presets[i] is the preset keyframe i was made from (two keyframes each)."""
    def weights(value) -> list[Keyframe]:
        return [Keyframe(t=f.t, yaw=float(value(p)), pitch=0, roll=0, fov=1) for f, p in zip(frames, presets)]
    locked = weights(lambda p: p.horizon == "locked")
    held = weights(lambda p: p.world and motion.world)
    aimed = weights(lambda p: p.target == "home" and motion.home_at(shot.start_s) is not None)
    north = motion.heading_at(shot.start_s) if motion.world else 0.0
    step = LEVEL_STEP_S / 2 if any(w.yaw for w in held + aimed) else LEVEL_STEP_S
    out, t = [], 0.0
    while True:
        t = min(t, shot.duration_s)
        base = _at(frames, t)
        video_t = shot.start_s + t
        hold, aim = _at(held, t).yaw, _at(aimed, t).yaw
        if hold > 0:                                # the aircraft turns, the picture does not
            base.yaw -= hold * (motion.heading_at(video_t) - north)
        if aim > 0 and motion.home_at(video_t) is not None:    # the picture stays on one place on the ground
            yaw, pitch = motion.home_at(video_t)
            base.yaw += aim * ((yaw - base.yaw + 180) % 360 - 180)
            base.pitch += aim * (pitch - base.pitch)
        lock = _at(locked, t).yaw
        amount = -(1.0 - lock) if stabilized else lock      # a stabilised source needs the bank put back
        bank = motion.bank_at(video_t)
        psi = math.radians(base.yaw)
        base.roll += ROLL_FOR_BANK * amount * bank * math.cos(psi)
        base.pitch += PITCH_FOR_BANK * amount * bank * math.sin(psi)
        base.t = round(t, 3)
        out.append(base)
        if t >= shot.duration_s:
            return out
        t += step


def shot_mount(mount: Mount, shot: Shot) -> Mount:
    """The mount plus the views made for this shot alone (AI framing: attrs["views"])."""
    own = shot.attrs.get("views")
    if not own:
        return mount
    return mount.model_copy(update={"presets": {**mount.presets, **{k: Preset.model_validate(v) for k, v in own.items()}}})


def plan_framing(reel: ReelPlan, profile: Profile, mount: Mount, telemetry: Telemetry | None = None,
                 calibration: Calibration | None = None) -> FramingPlan:
    """`source` identifies everything the plan was made from; `plan` keeps a
    hand-edited framing plan only while that stays the same."""
    out = profile.output
    stabilized = bool(calibration and calibration.stabilized)
    motion = Motion(telemetry)
    shots = []
    for s in reel.shots:
        own = shot_mount(mount, s)
        # The projection cannot change inside a shot. A plan that mixes them (a hand edit)
        # keeps the views in the projection of the first one, so the film is still made.
        first = own.presets[s.framing[0]].projection
        if any(own.presets[name].projection != first for name in s.framing):
            kept = [name for name in s.framing if own.presets[name].projection == first]
            print(f"plan: shot {s.index} ({s.scene_id}) mixed projections; kept {kept} of {s.framing}")
            s = s.model_copy(update={"framing": kept})
        def camera(names: list[str], size: tuple[int, int]) -> list[Keyframe]:
            frames = frame_shot(names, s.duration_s, own, profile.transition_s * s.speed, profile.safe_zone,
                                size, s.attrs, s.start_s, profile.max_turn_dps / s.speed, profile.max_track_dps / s.speed)
            made = [own.presets[name] for name in names for _ in (0, 1)]
            if len(made) != len(frames):            # a tracked view brings keyframes of its own: left as they are
                return frames
            level = any((p.horizon == "locked") != stabilized for p in made)
            world = motion.world and any(p.world or p.target for p in made)
            return animate(frames, made, s, motion, stabilized) if motion and (level or world) else frames

        second = [n for n in s.second if n in own.presets and own.presets[n].projection == "flat"]
        whole = (out.width, out.height)
        half = (out.width, out.height // 2)
        frames = camera(s.framing, half if second else whole)
        projections = {own.presets[name].projection for name in s.framing}
        shots.append(FramedShot(index=s.index, scene_id=s.scene_id, start_s=s.start_s, duration_s=s.duration_s,
                                keyframes=frames, recording=s.recording, speed=s.speed, type=s.type,
                                projection=projections.pop(), second=camera(second, half) if second else []))
    return FramingPlan(theme=reel.theme, length_s=reel.length_s, width=out.width, height=out.height,
                       fps=out.fps, shots=shots,
                       source=fingerprint(reel.model_dump_json(), mount.model_dump_json(), out.model_dump_json(),
                                          profile.safe_zone.model_dump_json(), profile.transition_s, stabilized,
                                          profile.max_turn_dps, profile.max_track_dps,
                                          telemetry.model_dump_json() if telemetry else "",
                                          ROLL_FOR_BANK, PITCH_FOR_BANK, PITCH_UP, "motion 2"))

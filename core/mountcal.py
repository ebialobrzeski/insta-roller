"""Where the camera points, worked out from its own sensors.

A camera that is not bolted to a known place (a suction cup on a canopy, moved
by hand in flight) cannot be described by fixed angles in a mount file. Two
things are measured instead, for any stretch of the recording:

  level  the tilt of the camera, from the direction of gravity: (pitch, roll)
         that make the horizon level, as holiday.level_of does for a selfie stick;
  nose   the yaw at which the aircraft's nose is, from the accelerometer and the
         flight log: when the log says the aircraft speeds up, the camera feels a
         push along the nose. The direction along which the horizontal
         acceleration follows the change of speed is the nose.

The nose can only be found where the speed changes (takeoff, landing, dives);
it is searched over a long window around the scene and cut at the moments the
camera was moved, which show up as a jump of the gravity direction.
"""
from __future__ import annotations

import numpy as np

from .holiday import level_of

MOVED_DEG = 10.0            # the gravity direction jumps by more than this: the camera was moved
MIN_CORRELATION = 0.25      # below this the acceleration does not follow the speed: no nose found


def per_second(imu: np.ndarray) -> np.ndarray:
    """IMU rows [t, ax, ay, az, ...] averaged to one row per whole second: [t, ax, ay, az]."""
    seconds = np.floor(imu[:, 0]).astype(int)
    first, last = seconds[0], seconds[-1]
    out = np.zeros((last - first + 1, 4))
    counts = np.bincount(seconds - first, minlength=len(out)).astype(float)
    for k in (1, 2, 3):
        out[:, k] = np.bincount(seconds - first, weights=imu[:, k], minlength=len(out)) / np.maximum(counts, 1)
    out[:, 0] = np.arange(first, last + 1)
    return out[counts > 0]


def _angle(a: np.ndarray, b: np.ndarray) -> float:
    cos = float(a.dot(b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9))
    return float(np.degrees(np.arccos(min(max(cos, -1.0), 1.0))))


def moves(acc: np.ndarray, window_s: int = 60) -> list[float]:
    """Times at which the camera was moved: the mean gravity direction of the
    minute before and of the minute after differ by more than MOVED_DEG."""
    found: list[float] = []
    step = window_s // 2
    for i in range(window_s, len(acc) - window_s, step):
        before, after = acc[i - window_s:i, 1:4].mean(axis=0), acc[i:i + window_s, 1:4].mean(axis=0)
        if _angle(before, after) > MOVED_DEG and (not found or acc[i, 0] - found[-1] > 2 * window_s):
            found.append(float(acc[i, 0]))
    return found


def level(acc: np.ndarray, start_s: float, end_s: float) -> tuple[float, float] | None:
    part = acc[(acc[:, 0] >= start_s) & (acc[:, 0] <= end_s)]
    return level_of(part[:, 1:4].mean(axis=0)) if len(part) >= 3 else None


def nose(acc: np.ndarray, speed: np.ndarray, start_s: float, end_s: float) -> tuple[float, float] | None:
    """(yaw of the nose in the levelled picture, correlation) over a stretch.
    speed: rows [t, m/s] at about 1 Hz, on the same clock as acc."""
    part = acc[(acc[:, 0] >= start_s) & (acc[:, 0] <= end_s)]
    if len(part) < 120 or len(speed) < 10:
        return None
    t = part[:, 0]
    v = np.interp(t, speed[:, 0], speed[:, 1])
    kernel = np.ones(5) / 5
    dv = np.gradient(np.convolve(v, kernel, mode="same"), t)
    a = np.column_stack([np.convolve(part[:, k], kernel, mode="same") for k in (1, 2, 3)])
    up = a.mean(axis=0)
    up /= max(np.linalg.norm(up), 1e-9)
    lens = np.array([0.0, 1.0, 0.0])                # yaw 0 of the levelled picture: the lens axis
    ahead = lens - lens.dot(up) * up
    ahead /= max(np.linalg.norm(ahead), 1e-9)
    right = np.cross(up, ahead)
    flat = a - np.outer(a.dot(up), up)
    x = np.column_stack([flat.dot(ahead), flat.dot(right)])
    x -= x.mean(axis=0)
    dv = dv - dv.mean()
    inside = slice(5, -5)                           # the smoothing is not valid at the ends
    coef, *_ = np.linalg.lstsq(x[inside], dv[inside], rcond=None)
    fit = x[inside].dot(coef)
    spread = float(np.std(fit) * np.std(dv[inside]))
    if spread < 1e-9:
        return None
    correlation = float(np.mean(fit * dv[inside]) / spread)
    # The sensor reports the push the other way round: the nose is opposite the fitted direction.
    yaw = (float(np.degrees(np.arctan2(coef[1], coef[0]))) + 180 + 180) % 360 - 180
    return round(yaw, 1), round(correlation, 3)


def nose_from_roll(imu: np.ndarray, start_s: float, end_s: float, hz: int = 5) -> tuple[float, float] | None:
    """(yaw of the nose in the levelled picture, correlation), from the gyroscope alone.
    An aircraft rolls about its nose before it turns: the horizontal direction about
    which the camera rotates when the turn rate is about to change is the nose.
    Works all through a flight, wherever the aircraft banks. imu: [t, ax, ay, az, gx, gy, gz]."""
    part = imu[(imu[:, 0] >= start_s) & (imu[:, 0] <= end_s)]
    rate = 1.0 / max(float(np.median(np.diff(part[:, 0]))), 1e-3) if len(part) > 2 else 0.0
    n = max(int(round(rate / hz)), 1)
    if len(part) < n * hz * 60:                     # under a minute: too little to tell
        return None
    p = part[:len(part) // n * n].reshape(-1, n, part.shape[1]).mean(axis=1)
    t, acc, gyro = p[:, 0], p[:, 1:4], p[:, 4:7]
    up = acc.mean(axis=0)
    up /= max(np.linalg.norm(up), 1e-9)
    lens = np.array([0.0, 1.0, 0.0])
    ahead = lens - lens.dot(up) * up
    ahead /= max(np.linalg.norm(ahead), 1e-9)
    right = np.cross(up, ahead)
    kernel = np.ones(2 * hz) / (2 * hz)

    def smooth(x: np.ndarray) -> np.ndarray:
        return np.convolve(x, kernel, mode="same")
    change = np.gradient(smooth(gyro.dot(up)), t)   # how the turn rate changes
    x = np.column_stack([smooth(gyro.dot(ahead)), smooth(gyro.dot(right))])
    x -= x.mean(axis=0)
    change = change - change.mean()
    inside = slice(3 * hz, -3 * hz)
    coef, *_ = np.linalg.lstsq(x[inside], change[inside], rcond=None)
    fit = x[inside].dot(coef)
    spread = float(np.std(fit) * np.std(change[inside]))
    if spread < 1e-12:
        return None
    return (round(float(np.degrees(np.arctan2(coef[1], coef[0]))), 1),
            round(float(np.mean(fit * change[inside]) / spread), 3))


ROLL_CORRELATION = 0.3      # the roll method is trusted from here
AROUND_S = 420.0            # the nose of a scene is looked for this far to both sides of it


def calibrate(imu: np.ndarray, spans: list[tuple[float, float]],
              speed: np.ndarray | None = None) -> list[tuple[float, float, float] | None]:
    """For each (start, end) of a scene: (pitch, roll, nose yaw), or None when the
    sensors do not say. The level is the scene's own. The nose comes from the roll
    of the aircraft around the scene, then from the whole stretch between two moves
    of the camera, then from the acceleration against the logged speed."""
    if len(imu) < 500:
        return [None] * len(spans)
    acc = per_second(imu)
    cuts = [float(acc[0, 0]), *moves(acc), float(acc[-1, 0]) + 1]
    whole: dict[int, float | None] = {}
    out = []
    for start, end in spans:
        middle = (start + end) / 2
        k = max(i for i in range(len(cuts) - 1) if cuts[i] <= middle or i == 0)
        lo, hi = cuts[k] + 5, cuts[k + 1] - 5
        near = nose_from_roll(imu, max(lo, start - AROUND_S), min(hi, end + AROUND_S))
        yaw = near[0] if near and near[1] >= ROLL_CORRELATION else None
        if yaw is None:
            if k not in whole:
                found = nose_from_roll(imu, lo, hi)
                whole[k] = found[0] if found and found[1] >= ROLL_CORRELATION else None
                if whole[k] is None and speed is not None:
                    found = nose(acc, speed, lo, hi)
                    whole[k] = found[0] if found and found[1] >= 0.5 else None
            yaw = whole[k]
        tilt = level(acc, max(start, lo), min(end, hi)) if min(end, hi) - max(start, lo) >= 3 else level(acc, start, end)
        out.append(None if tilt is None or yaw is None else (tilt[0], tilt[1], yaw))
    return out

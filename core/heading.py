"""Where the aircraft points and where it is, second by second.

A view can then be held on the world instead of on the aircraft: on a compass
direction, or on a point on the ground, while the aircraft turns under it.

The course over ground from the log is right on average but coarse (one fix a
second, and it lags in a turn); the gyroscope is smooth and immediate but
drifts. The heading is the gyroscope's turning with its slow error taken from
the log (a complementary filter).
"""
from __future__ import annotations

import math

import numpy as np

BLEND_S = 40            # the gyroscope is trusted over shorter times than this, the log over longer


def course(t: np.ndarray, lat: np.ndarray, lon: np.ndarray, span: int = 2) -> np.ndarray:
    """Course over ground at every fix, degrees, unwrapped (it may run past 360)."""
    n = len(t)
    a, b = np.clip(np.arange(n) - span, 0, n - 1), np.clip(np.arange(n) + span, 0, n - 1)
    north = np.radians(lat[b] - lat[a])
    east = np.radians(lon[b] - lon[a]) * np.cos(np.radians(lat))
    angle = np.degrees(np.arctan2(east, north))
    moving = np.hypot(north, east) * 6371000.0 > 2.0 * span      # standing still: keep the last course
    for i in range(1, n):
        if not moving[i]:
            angle[i] = angle[i - 1]
    return np.degrees(np.unwrap(np.radians(angle)))


def headings(fix_t: np.ndarray, lat: np.ndarray, lon: np.ndarray, at_utc: np.ndarray,
             gyro_utc: np.ndarray | None = None, turn_dps: np.ndarray | None = None) -> np.ndarray:
    """Heading (degrees, 0 = north, unwrapped) at the times at_utc.
    gyro_utc / turn_dps: turn rate about the vertical from the camera, any rate; without it the course is returned."""
    track = np.interp(at_utc, fix_t, course(fix_t, lat, lon))
    if gyro_utc is None or turn_dps is None or len(gyro_utc) < 10 or len(at_utc) < 3:
        return track
    step = np.gradient(gyro_utc)
    turned = np.interp(at_utc, gyro_utc, np.cumsum(turn_dps * step))
    rate_log, rate_gyro = np.gradient(track, at_utc), np.gradient(turned, at_utc)
    if float(np.sum(rate_log * rate_gyro)) < 0:                   # the sensor counts the other way round
        turned = -turned
    error = track - turned
    width = max(int(BLEND_S / max(float(np.median(np.diff(at_utc))), 1e-3)), 1)
    padded = np.pad(error, width, mode="edge")
    slow = np.convolve(padded, np.ones(2 * width + 1) / (2 * width + 1), mode="same")[width:-width]
    return turned + slow


def bearing_down(lat: float, lon: float, alt: float, to: tuple[float, float, float]) -> tuple[float, float, float]:
    """(bearing in degrees, angle below the horizon in degrees, distance in m) from a position to a point."""
    north = math.radians(to[0] - lat) * 6371000.0
    east = math.radians(to[1] - lon) * 6371000.0 * math.cos(math.radians(lat))
    distance = math.hypot(north, east)
    return math.degrees(math.atan2(east, north)) % 360, math.degrees(math.atan2(alt - to[2], max(distance, 1.0))), distance

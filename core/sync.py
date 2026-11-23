"""Tie the video clock to UTC.

In order of preference:
1. turn-rate correlation between the camera's gyroscope and the flight track
   (IGC or the camera's own GPS), searched around a first guess,
2. the position of the GPS records in the camera file,
3. the camera clock plus a manual offset.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import telemetry
from .telemetry import CameraData

STEP_S = 0.1


@dataclass
class SyncResult:
    video_start_utc: float
    method: str
    correlation: float | None = None


def track_turn_rate(t: np.ndarray, lat: np.ndarray, lon: np.ndarray, min_speed_ms: float = 8.0
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Turn rate of a ground track in deg/s (positive = right), zero while slow."""
    east = np.gradient(lon) * np.cos(np.radians(lat)) * 111320.0
    north = np.gradient(lat) * 111320.0
    dt = np.maximum(np.gradient(t), 1e-3)
    speed = np.hypot(east, north) / dt
    course = np.degrees(np.unwrap(np.arctan2(east, north)))
    rate = np.gradient(course, t)
    rate[speed < min_speed_ms] = 0.0
    return rate, speed


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    return np.convolve(x, np.ones(n) / n, "same")


def correlate(track_t: np.ndarray, track_rate: np.ndarray, cam: CameraData, guess_utc: float,
              duration_s: float, search_s: float) -> tuple[float, float]:
    """Returns (video start UTC, correlation) that best matches gyro turns to the track."""
    grid = np.arange(0.0, duration_s, STEP_S)
    gyro = _smooth(np.interp(grid, cam.imu[:, 0], telemetry.turn_about_vertical(cam.imu)), 10)
    pad = int(search_s / STEP_S)
    wide = np.arange(-pad, len(grid) + pad) * STEP_S + guess_utc
    track = _smooth(np.interp(wide, track_t, track_rate, left=0.0, right=0.0), 10)
    g = gyro - gyro.mean()
    # Correlation of the gyro series against every shift of the track series.
    num = np.correlate(track, g, "valid")
    ones = np.ones(len(g))
    s1 = np.correlate(track, ones, "valid")
    s2 = np.correlate(track * track, ones, "valid")
    var = np.maximum(s2 - s1 * s1 / len(g), 1e-9)
    corr = num / np.sqrt(var * (g @ g) + 1e-9)
    k = int(np.argmax(np.abs(corr)))
    shift = float(k)
    if 0 < k < len(corr) - 1:        # parabola through the peak for sub-step precision
        a, b, c = np.abs(corr[k - 1:k + 2])
        if a - 2 * b + c:
            shift += 0.5 * (a - c) / (a - 2 * b + c)
    return guess_utc + (shift - pad) * STEP_S, float(corr[k])


def synchronise(cam: CameraData, duration_s: float, camera_clock_utc: float | None,
                track: tuple[np.ndarray, np.ndarray, np.ndarray] | None, own_gps: bool,
                search_s: float, min_correlation: float) -> SyncResult | None:
    """track = (utc, lat, lon) of the flight log used for the scenes; own_gps = it is the camera's GPS."""
    guess, method = None, ""
    if cam.gps_start_utc is not None:
        guess, method = cam.gps_start_utc, "camera GPS"
    elif cam.has_gps and camera_clock_utc is not None:
        guess, method = camera_clock_utc, "camera clock"
    elif camera_clock_utc is not None:
        guess, method = camera_clock_utc, "camera clock"
    if guess is None:
        return None
    if track is not None and cam.has_imu:
        t, lat, lon = track
        rate, _ = track_turn_rate(t, lat, lon)
        # The camera's own GPS can only be a few seconds off; a foreign clock much more.
        span = 15.0 if (own_gps and cam.gps_start_utc is not None) else search_s
        start, corr = correlate(t, rate, cam, guess, duration_s, span)
        if abs(corr) >= min_correlation:
            return SyncResult(start, "gyro turns matched to the track", round(abs(corr), 3))
    return SyncResult(guess, method)

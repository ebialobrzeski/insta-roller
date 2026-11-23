"""Synthetic test flight with known phase times (seconds from the first fix)."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

START = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)

# (duration s, speed m/s, turn rate deg/s, vario m/s)
LEGS = [
    (60, 0, 0, 0),         # parked
    (20, 15, 0, 0),        # ground roll
    (300, 30, 0, 2.5),     # aerotow, straight north
    (60, 25, 0, -1.0),     # glide
    (240, 22, -18, 2.0),   # thermal, left
    (180, 30, 0, -1.2),    # glide
    (180, 22, 18, 1.0),    # thermal, right
]
TRUTH = {"takeoff": 60, "release": 380, "thermal_left": (440, 680), "thermal_right": (860, 1040)}
GROUND_ALT = 100.0


def build(landing: bool = True) -> tuple[list[tuple[int, float, float, float]], dict]:
    """Returns (second, lat, lon, alt) samples at 1 Hz and the truth table."""
    lat, lon, alt, heading = 52.0, 21.0, GROUND_ALT, 0.0
    t = 0
    out = [(t, lat, lon, alt)]

    def step(speed, turn, vario):
        nonlocal lat, lon, alt, heading, t
        heading = (heading + turn) % 360
        lat += speed * math.cos(math.radians(heading)) / 111320.0
        lon += speed * math.sin(math.radians(heading)) / (111320.0 * math.cos(math.radians(lat)))
        alt += vario
        t += 1
        out.append((t, lat, lon, alt))

    for duration, speed, turn, vario in LEGS:
        for _ in range(duration):
            step(speed, turn, vario)
    truth = dict(TRUTH)
    if landing:
        while alt > GROUND_ALT:
            step(28, 0, -1.5)
        alt = GROUND_ALT
        truth["touchdown"] = t
        for speed in range(26, 0, -2):
            step(speed, 0, 0)
        for _ in range(60):
            step(0, 0, 0)
    else:
        for _ in range(300):
            step(28, 0, -1.0)
    return out, truth


def _coord(value: float, deg_digits: int, pos: str, neg: str) -> str:
    hemi = pos if value >= 0 else neg
    value = abs(value)
    deg = int(value)
    minutes = round((value - deg) * 60000)
    return f"{deg:0{deg_digits}d}{minutes:05d}{hemi}"


def write_igc(path: Path, landing: bool = True) -> dict:
    samples, truth = build(landing)
    lines = ["AXXXSYN", f"HFDTE{START:%d%m%y}"]
    base = START.hour * 3600 + START.minute * 60 + START.second
    for t, lat, lon, alt in samples:
        tod = base + t
        a = int(round(alt))
        lines.append(f"B{tod // 3600:02d}{tod % 3600 // 60:02d}{tod % 60:02d}"
                     f"{_coord(lat, 2, 'N', 'S')}{_coord(lon, 3, 'E', 'W')}A{a:05d}{a:05d}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return truth

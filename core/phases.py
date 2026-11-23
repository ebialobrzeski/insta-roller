"""Flight phase detection from IGC fixes.

Phases: takeoff, tow (launch climb), release (end of the launch climb),
thermal (with direction and climb rate), glide, circuit, landing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Detection
from .igc import Fix


@dataclass
class Phase:
    type: str
    start: float          # unix time
    end: float
    attrs: dict = field(default_factory=dict)


@dataclass
class Track:
    """Fixes resampled to 1 Hz with derived signals."""
    t0: float
    alt: list[float]
    speed: list[float]    # ground speed, m/s
    turn: list[float]     # smoothed turn rate, deg/s, positive = right
    vario: list[float]    # smoothed, m/s

    def __len__(self) -> int:
        return len(self.alt)


def _dist_bearing(lat1, lon1, lat2, lon2) -> tuple[float, float]:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = dl * math.cos((p1 + p2) / 2)
    y = p2 - p1
    return r * math.hypot(x, y), math.degrees(math.atan2(x, y)) % 360


def _smooth(values: list[float], window: int) -> list[float]:
    half = max(window // 2, 0)
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    out = []
    n = len(values)
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append((prefix[b] - prefix[a]) / (b - a))
    return out


def build_track(fixes: list[Fix], p: Detection) -> Track:
    t0 = fixes[0].t
    n = int(fixes[-1].t - t0) + 1
    lat, lon, alt = [], [], []
    j = 0
    for i in range(n):
        t = t0 + i
        while j < len(fixes) - 2 and fixes[j + 1].t <= t:
            j += 1
        a, b = fixes[j], fixes[j + 1]
        k = min(max((t - a.t) / (b.t - a.t), 0.0), 1.0)
        lat.append(a.lat + (b.lat - a.lat) * k)
        lon.append(a.lon + (b.lon - a.lon) * k)
        alt.append(a.alt + (b.alt - a.alt) * k)

    w = 2
    speed, heading = [], []
    last_heading = 0.0
    for i in range(n):
        a, b = max(0, i - w), min(n - 1, i + w)
        d, brg = _dist_bearing(lat[a], lon[a], lat[b], lon[b])
        v = d / max(b - a, 1)
        speed.append(v)
        if v > 3.0:
            last_heading = brg
        heading.append(last_heading)

    raw_turn = [0.0]
    for i in range(1, n):
        raw_turn.append((heading[i] - heading[i - 1] + 180) % 360 - 180)
    turn = _smooth(raw_turn, p.turn_smooth_s)

    half = max(p.vario_smooth_s // 2, 1)
    vario = []
    for i in range(n):
        a, b = max(0, i - half), min(n - 1, i + half)
        vario.append((alt[b] - alt[a]) / max(b - a, 1))
    return Track(t0, alt, speed, turn, vario)


def _first_sustained(values: list[float], threshold: float, hold: int, start: int = 0) -> int | None:
    run = 0
    for i in range(start, len(values)):
        run = run + 1 if values[i] > threshold else 0
        if run >= hold:
            return i - hold + 1
    return None


def _last_sustained(values: list[float], threshold: float, hold: int) -> int | None:
    run = 0
    for i in range(len(values) - 1, -1, -1):
        run = run + 1 if values[i] > threshold else 0
        if run >= hold:
            return i + hold - 1
    return None


def detect_phases(fixes: list[Fix], p: Detection, aircraft: str = "glider",
                  engine_off_utc: float | None = None, track: Track | None = None) -> list[Phase]:
    """aircraft "motorglider": the launch climb is `climb` and ends with `engine_off`,
    at engine_off_utc when the sound gave it away, else where the climb stops."""
    tr = track or build_track(fixes, p)
    n = len(tr)
    T = lambda i: tr.t0 + i  # noqa: E731
    phases: list[Phase] = []

    fast = _first_sustained(tr.speed, p.takeoff_speed_ms, p.speed_hold_s)
    if fast is None:
        return phases
    roll = fast
    while roll > 0 and tr.speed[roll - 1] > 2.0:
        roll -= 1
    airborne_from_start = roll == 0 and tr.speed[0] > p.takeoff_speed_ms
    ground_ref = min(tr.alt) if airborne_from_start else tr.alt[roll]

    last_fast = _last_sustained(tr.speed, p.landing_speed_ms, p.speed_hold_s)
    landed = last_fast is not None and last_fast < n - p.speed_hold_s
    touchdown = last_fast if landed else None
    flight_end = touchdown if landed else n - 1

    # End of the launch climb: tow release, winch release or engine off.
    release = None
    if not airborne_from_start:
        i = roll + p.release_min_after_takeoff_s
        while i < flight_end and tr.alt[i] < tr.alt[roll] + p.release_min_gain_m:
            i += 1                      # still on the ground roll
        while i + p.release_window_s < flight_end:
            window = tr.alt[i + p.release_window_s] - tr.alt[i]
            if window / p.release_window_s < p.release_vario_ms:
                top = tr.alt[i:i + p.release_window_s + 1]
                release = i + top.index(max(top))       # the top of the climb
                break
            i += 1
        if engine_off_utc is not None and roll < engine_off_utc - tr.t0 < flight_end:
            release = int(engine_off_utc - tr.t0)
        takeoff_end = min(roll + int(p.takeoff_scene_s), release or flight_end)
        phases.append(Phase("takeoff", T(roll), T(takeoff_end)))
        if release is not None:
            if release - takeoff_end >= 5:
                phases.append(Phase("tow", T(takeoff_end), T(release),
                                    {"alt_gain_m": round(tr.alt[release] - tr.alt[roll])}))
            half = p.release_scene_s / 2
            phases.append(Phase("release", T(release) - half, T(release) + half,
                                {"alt_m": round(tr.alt[release])}))
    free_start = release if release is not None else (roll if airborne_from_start else flight_end)

    # Circuit and landing.
    circuit_start = flight_end
    if landed:
        ground = tr.alt[touchdown]
        final_start = max(free_start, touchdown - int(p.final_s))
        circuit_start = final_start
        for i in range(final_start, free_start, -1):
            if tr.alt[i] >= ground + p.circuit_agl_m or final_start - i >= p.circuit_max_s:
                break
            circuit_start = i
        if final_start - circuit_start >= 5:
            phases.append(Phase("circuit", T(circuit_start), T(final_start)))
        phases.append(Phase("landing", T(final_start), min(T(touchdown) + p.rollout_s, T(n - 1))))

    # Thermals: sustained circling between the release and the circuit.
    runs: list[list[int]] = []
    for i in range(free_start, circuit_start):
        if abs(tr.turn[i]) >= p.circling_rate_dps:
            if runs and i - runs[-1][1] <= p.circling_gap_s:
                runs[-1][1] = i
            else:
                runs.append([i, i])
    thermals: list[tuple[int, int]] = []
    for a, b in runs:
        turned = sum(tr.turn[a:b + 1])
        if b - a >= p.thermal_min_s and abs(turned) >= p.thermal_min_turn_deg:
            thermals.append((a, b))
            phases.append(Phase("thermal", T(a), T(b), {
                "direction": "right" if turned > 0 else "left",
                "turns": round(abs(turned) / 360, 1),
                "climb_ms": round((tr.alt[b] - tr.alt[a]) / (b - a), 2),
                "alt_gain_m": round(tr.alt[b] - tr.alt[a]),
                "agl_m": round(tr.alt[a] - ground_ref),     # height above the airfield at entry
            }))

    # Glides: what is left between the release, thermals and the circuit.
    edges = [free_start] + [x for ab in thermals for x in ab] + [circuit_start]
    for a, b in zip(edges[0::2], edges[1::2]):
        if b - a >= p.glide_min_s:
            phases.append(Phase("glide", T(a), T(b), {
                "alt_loss_m": round(tr.alt[a] - tr.alt[b]),
                "speed_kmh": round(sum(tr.speed[a:b]) / (b - a) * 3.6),
            }))

    if aircraft == "motorglider":
        for ph in phases:
            ph.type = {"tow": "climb", "release": "engine_off"}.get(ph.type, ph.type)
    phases.sort(key=lambda ph: ph.start)
    return phases


def bank_deg(speed_ms: float, turn_dps: float) -> float:
    """Bank of a coordinated turn; positive = right wing down."""
    return math.degrees(math.atan(speed_ms * math.radians(turn_dps) / 9.81))


def phases_from_gyro(turn_dps: list[float], t0: float, p: Detection) -> list[Phase]:
    """Last resort without any position data: circling and straight flight from
    the gyroscope alone. turn_dps is the turn rate about the vertical at 1 Hz."""
    turn = _smooth(turn_dps, p.turn_smooth_s)
    n = len(turn)
    runs: list[list[int]] = []
    for i in range(n):
        if abs(turn[i]) >= p.circling_rate_dps:
            if runs and i - runs[-1][1] <= p.circling_gap_s:
                runs[-1][1] = i
            else:
                runs.append([i, i])
    phases, edges = [], [0]
    for a, b in runs:
        turned = sum(turn[a:b + 1])
        if b - a >= p.thermal_min_s and abs(turned) >= p.thermal_min_turn_deg:
            phases.append(Phase("thermal", t0 + a, t0 + b, {
                "direction": "right" if turned > 0 else "left", "turns": round(abs(turned) / 360, 1)}))
            edges += [a, b]
    edges.append(n - 1)
    for a, b in zip(edges[0::2], edges[1::2]):
        if b - a >= p.glide_min_s:
            phases.append(Phase("glide", t0 + a, t0 + b, {}))
    phases.sort(key=lambda ph: ph.start)
    return phases

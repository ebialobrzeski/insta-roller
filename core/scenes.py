"""Turn flight phases into scored scenes on the video timeline."""
from __future__ import annotations

from .config import Profile
from .contracts import Scene, TelemetrySample
from .phases import Phase, Track, bank_deg

MIN_SCENE_S = 2.0


def score(phase: Phase, profile: Profile) -> float:
    base = profile.scene_types[phase.type].base_score
    if phase.type == "thermal" and "climb_ms" in phase.attrs:
        climb = max(phase.attrs.get("climb_ms", 0.0), 0.0)
        base += (1.0 - base) * min(climb / profile.thermal_good_climb_ms, 1.0)
        base += profile.thermal_low_bonus * low_factor(phase.attrs, profile)
    return round(min(base, 1.0), 3)


def low_factor(attrs: dict, profile: Profile) -> float:
    """1 at ground level, 0 at or above the low-thermal limit."""
    agl = attrs.get("agl_m")
    if agl is None:
        return 0.0
    return min(max(1.0 - agl / profile.thermal_low_agl_m, 0.0), 1.0)


def build_scenes(phases: list[Phase], video_start_utc: float, duration_s: float,
                 profile: Profile) -> tuple[list[Scene], list[str]]:
    """Returns the scenes inside the recording and the phase types it misses."""
    scenes: list[Scene] = []
    missing: list[str] = []
    for i, ph in enumerate(phases):
        if ph.type not in profile.scene_types:
            continue
        start = max(ph.start - video_start_utc, 0.0)
        end = min(ph.end - video_start_utc, duration_s)
        if end - start < MIN_SCENE_S:
            if ph.type not in missing:
                missing.append(ph.type)
            continue
        attrs = dict(ph.attrs)
        if ph.type == "thermal" and low_factor(attrs, profile) > 0:
            attrs["low"] = True
        if end - start < (ph.end - ph.start) - 1.0:
            attrs["truncated"] = True
        scenes.append(Scene(id=f"{i:03d}_{ph.type}", type=ph.type, start_s=round(start, 2),
                            end_s=round(end, 2), score=score(ph, profile), attrs=attrs))
    present = {s.type for s in scenes}
    return scenes, [t for t in missing if t not in present]


def split_long(scenes: list[Scene], chunk_s: float, types: tuple[str, ...] = ("glide", "climb", "thermal")) -> list[Scene]:
    """A scene gives a reel one shot. An hour of straight flight as one scene would
    give one shot of it, so long glides, climbs and thermals are cut into parts of
    about chunk_s; each part is a scene of its own. Ids are numbered again."""
    out: list[Scene] = []
    for s in scenes:
        length = s.end_s - s.start_s
        parts = int(round(length / chunk_s)) if chunk_s > 0 and s.type in types and length >= 2 * chunk_s else 1
        for k in range(parts):
            a, b = s.start_s + length * k / parts, s.start_s + length * (k + 1) / parts
            out.append(s.model_copy(update={"start_s": round(a, 2), "end_s": round(b, 2), "attrs": dict(s.attrs)}))
    return [s.model_copy(update={"id": f"{i:03d}_{s.type}"}) for i, s in enumerate(out)]


def telemetry_samples(track: Track, video_start_utc: float, duration_s: float) -> list[TelemetrySample]:
    """Speed, altitude, vario and bank for every second of the video the track covers."""
    out = []
    for i in range(len(track)):
        t = track.t0 + i - video_start_utc
        if 0 <= t <= duration_s:
            out.append(TelemetrySample(t=round(t, 2), speed_kmh=round(track.speed[i] * 3.6, 1),
                                       alt_m=round(track.alt[i], 1), vario_ms=round(track.vario[i], 2),
                                       bank_deg=round(bank_deg(track.speed[i], track.turn[i]), 1)))
    return out

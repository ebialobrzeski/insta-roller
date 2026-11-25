"""Looking through the whole recording for things worth showing.

The scenes come from sensors: they know a thermal from a glide, not a lake
from a haze. A 360 recording has the whole surroundings at every moment, so a
vision model is shown the sphere every minute or so and says where something
is worth a shot: water, a town, a striking cloud, another aircraft, the sun
low over the land. Its picks become scenes of the type "sight", aimed at what
it saw.

Costs one request per ten panoramas; off unless ai_framing.scout is on.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import aiframe
from .config import Profile
from .contracts import Scene
from .ingest import fingerprint

BATCH = 10
HALF_S = 6.0                # a sight is a scene this long to both sides of the moment it was seen
MIN_SCORE = 6               # below this a moment is not worth a shot of its own


def moments(scenes: list[Scene], every_s: float) -> list[float]:
    """Video times to look at: evenly through the flight, between its first and last scene."""
    if not scenes or every_s <= 0:
        return []
    first, last = min(s.start_s for s in scenes) + every_s / 2, max(s.end_s for s in scenes) - 10
    out, t = [], first
    while t < last:
        out.append(round(t, 1))
        t += every_s
    return out


def request_body(profile: Profile, numbered: list[tuple[int, object, Path]]) -> dict:
    if profile.kind == "flight":
        what = ("These are panoramas of the whole 360-degree sphere at moments of a glider flight, each with a grid of "
                "yaw (left to right, -180 to 180, 0 = straight ahead) and pitch (0 = horizon, negative = down). The "
                "cockpit, the wings and the pilot are in every one of them and are not what we look for.\n"
                "For each panorama say how much the WORLD OUTSIDE at that moment deserves a shot of its own in a "
                "short film, from 0 to 10: water, a river, a town or a striking building, a coast, mountains, a "
                "dramatic cloud or light, another aircraft, the airfield seen from above score high; plain haze, "
                "even fields and empty sky score low. Be strict: most moments of a flight are ordinary.\n")
    else:
        what = ("These are panoramas of the whole 360-degree sphere at moments of a holiday, filmed with a camera on a "
                "selfie stick, each with a grid of yaw (left to right, -180 to 180) and pitch (0 = horizon, negative = "
                "down, positive = up). The person holding the stick is in every one of them.\n"
                "For each panorama say how much THE PLACE at that moment deserves a shot of its own in a short film "
                "of the trip, from 0 to 10, and where to point the camera. A famous or striking building, a monument, "
                "a tower, a cathedral, a square, a ship, a harbour, the sea, a beach, mountains, a sunset score high, "
                "the more so the better they are seen. A plain street, a wall, a queue, the backs of a crowd, a "
                "corridor or a room with nothing in it score low. Give the yaw and pitch of the MIDDLE of the thing "
                "worth showing (for a tall building: the middle of its height). Name it if you recognise it.\n")
    content = [{"type": "text", "text": what +
                "Answer with JSON only: {\"moments\": [{\"n\": <number of the panorama>, \"score\": <0-10>, "
                "\"what\": \"<in Polish, a few words: what is worth showing>\", \"yaw\": <deg>, \"pitch\": <deg>}]}"}]
    for n, at, path in numbered:
        content.append({"type": "text", "text": f"Panorama {n}"})
        content.append(aiframe._image(aiframe.pano_sheet(path)))
    return {"model": profile.ai_framing.model, "temperature": 0.2, "messages": [{"role": "user", "content": content}]}


def parse_reply(text: str, numbered: list[tuple[int, float, Path]]) -> list[dict]:
    match = re.search(r"\{.*\}", text, re.S)
    try:
        rows = json.loads(match.group(0))["moments"] if match else []
    except (ValueError, KeyError, TypeError):
        rows = []
    times = {n: at for n, at, _ in numbered}
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            n, score = int(row["n"]), float(row["score"])
            yaw, pitch = float(row.get("yaw", 0.0)), float(row.get("pitch", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if n in times and 0 <= score <= 10:
            out.append({"t": times[n], "score": score, "what": str(row.get("what", ""))[:80],
                        "yaw": round((yaw + 180) % 360 - 180, 1), "pitch": round(min(max(pitch, -85.0), 60.0), 1)})
    return out


def pick(found: list[dict], limit: int, apart_s: float) -> list[dict]:
    """The best moments, not two of them close together."""
    kept: list[dict] = []
    for item in sorted(found, key=lambda f: -f["score"]):
        if item["score"] >= MIN_SCORE and len(kept) < limit and all(abs(item["t"] - k["t"]) >= apart_s for k in kept):
            kept.append(item)
    return sorted(kept, key=lambda f: f["t"])


def as_scenes(picked: list[dict], scenes: list[Scene], duration_s: float, base_score: float) -> list[Scene]:
    """Scenes of the type "sight". Each takes the camera position (level) of the scene it lies in."""
    out = []
    for i, item in enumerate(picked):
        at = item["t"]
        inside = min(scenes, key=lambda s: 0 if s.start_s <= at <= s.end_s else min(abs(s.start_s - at), abs(s.end_s - at)))
        attrs = {"what": item["what"], "yaw_sight": item["yaw"], "pitch_sight": item["pitch"], "seen": item["score"]}
        if "level" in inside.attrs:
            attrs["level"] = inside.attrs["level"]
        out.append(Scene(id=f"s{i:02d}_sight", type="sight", start_s=round(max(at - HALF_S, 0.0), 2),
                         end_s=round(min(at + HALF_S, duration_s), 2), recording=inside.recording,
                         score=round(min(base_score + (item["score"] - MIN_SCORE) * 0.05, 1.0), 3), attrs=attrs))
    return out


def scout(rec, mount, profile: Profile, scenes: list[Scene], duration_s: float, work: Path, log: Path,
          progress=print, post=None) -> list[Scene]:
    """Sight scenes for a recording. Remembered beside the work files, so asking twice costs nothing."""
    from . import render
    from .contracts import Keyframe
    cfg = profile.ai_framing
    times = moments(scenes, cfg.scout_every_s)
    if not times or "sight" not in profile.scene_types:
        return []
    folder = work / "ai_framing"
    folder.mkdir(parents=True, exist_ok=True)
    levels = {s.id: s.attrs.get("level") for s in scenes}

    def level_at(at: float):
        inside = min(scenes, key=lambda s: 0 if s.start_s <= at <= s.end_s else min(abs(s.start_s - at), abs(s.end_s - at)))
        return levels.get(inside.id)
    key = fingerprint(rec.id, cfg.model, cfg.scout_every_s, times, mount.model_dump_json(include={"calibration"}),
                      json.dumps(levels, sort_keys=True), "scout 1")
    cache = folder / f"scout_{key}.json"
    if cache.exists():
        found = json.loads(cache.read_text(encoding="utf-8"))
    else:
        post = post or aiframe.poster()
        render.spheres([(rec, at, level_at(at)) for at in times], mount, work, log)
        numbered = []
        for n, at in enumerate(times, 1):
            path = folder / f"scoutpano_{fingerprint(rec.id, at, level_at(at), mount.model_dump_json(include={'calibration'}))}.jpg"
            if not path.exists():
                render.still(rec, mount, at, Keyframe(t=0, yaw=0, pitch=0, roll=0, fov=100), (768, 384), path, work, log,
                             level=level_at(at), projection="pano")
            numbered.append((n, at, path))
            print(f"  shot {n}/{len(times)}  scout", flush=True)
        batches = [numbered[i:i + BATCH] for i in range(0, len(numbered), BATCH)]
        print(f"scout: asking the model about {len(numbered)} panoramas", flush=True)
        found = [item for part in aiframe.together(
            [lambda batch=batch: parse_reply(post(request_body(profile, batch)), batch) for batch in batches],
            lambda n, of: print(f"  part {n}/{of}  scout answers", flush=True)) for item in part]
        cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    picked = pick(found, cfg.scout_max, cfg.scout_apart_s)
    progress(f"scout: looked at {len(times)} moments, kept {len(picked)} sights")
    return as_scenes(picked, scenes, duration_s, profile.scene_types["sight"].base_score)


def clip_moments(start_s: float, end_s: float, every_s: float) -> list[float]:
    """Times to look at inside one stretch of a clip: evenly, and at least its middle."""
    length = end_s - start_s
    count = max(int(length // max(every_s, 1.0)), 1)
    return [round(start_s + length * (i + 0.5) / count, 1) for i in range(count)]


def scout_clips(recordings: dict, mount, profile: Profile, scenes: list[Scene], work: Path, log: Path,
                progress=print, post=None, limit: int = 240) -> list[Scene]:
    """Sight scenes for a holiday: every usable stretch of every clip is looked at, and the best
    moments of each day are kept. recordings: id -> Recording. scenes: what the sensors found."""
    from . import render
    from .contracts import Keyframe
    cfg = profile.ai_framing
    if not scenes or "sight" not in profile.scene_types:
        return []
    every = cfg.scout_every_holiday_s
    wanted = [(s, at) for s in scenes for at in clip_moments(s.start_s, s.end_s, every)]
    while len(wanted) > limit:                      # a long trip: look less often rather than send hundreds
        every *= 1.5
        wanted = [(s, at) for s in scenes for at in clip_moments(s.start_s, s.end_s, every)]
    folder = work / "ai_framing"
    folder.mkdir(parents=True, exist_ok=True)
    key = fingerprint(cfg.model, [(s.recording, at, s.attrs.get("level")) for s, at in wanted],
                      mount.model_dump_json(include={"calibration"}), "scout clips 1")
    cache = folder / f"scout_{key}.json"
    if cache.exists():
        found = json.loads(cache.read_text(encoding="utf-8"))
    else:
        post = post or aiframe.poster()
        render.spheres([(recordings[scene.recording], at, scene.attrs.get("level")) for scene, at in wanted], mount, work, log)
        numbered = []
        for n, (scene, at) in enumerate(wanted, 1):
            rec = recordings[scene.recording]
            level = scene.attrs.get("level")
            path = folder / f"scoutpano_{fingerprint(rec.id, at, level, mount.model_dump_json(include={'calibration'}))}.jpg"
            if not path.exists():
                render.still(rec, mount, at, Keyframe(t=0, yaw=0, pitch=0, roll=0, fov=100), (768, 384), path, work, log,
                             level=tuple(level) if level else None, projection="pano")
            numbered.append((n, n - 1, path))       # the "time" is the place in `wanted`
            print(f"  shot {n}/{len(wanted)}  scout", flush=True)
        batches = [numbered[i:i + BATCH] for i in range(0, len(numbered), BATCH)]
        print(f"scout: asking the model about {len(numbered)} panoramas", flush=True)
        found = [item for part in aiframe.together(
            [lambda batch=batch: parse_reply(post(request_body(profile, batch)), batch) for batch in batches],
            lambda n, of: print(f"  part {n}/{of}  scout answers", flush=True)) for item in part]
        cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    by_day: dict[str, list[dict]] = {}
    for item in found:
        scene, at = wanted[int(item["t"])]
        by_day.setdefault(scene.attrs.get("day", ""), []).append({**item, "scene": scene, "at": at})
    out, base = [], profile.scene_types["sight"].base_score
    for day in sorted(by_day):
        kept: list[dict] = []
        for item in sorted(by_day[day], key=lambda f: -f["score"]):     # the best of the day, not two from the same seconds
            near = any(k["scene"].recording == item["scene"].recording and abs(k["at"] - item["at"]) < 2 * HALF_S for k in kept)
            if item["score"] >= MIN_SCORE and len(kept) < cfg.scout_max and not near:
                kept.append(item)
        for item in sorted(kept, key=lambda k: (k["scene"].recording, k["at"])):
            scene, at = item["scene"], item["at"]
            attrs = {"what": item["what"], "yaw_sight": item["yaw"], "pitch_sight": item["pitch"], "seen": item["score"],
                     **{k: scene.attrs[k] for k in ("day", "level") if k in scene.attrs}}
            out.append(Scene(id=f"s{len(out):02d}_sight", type="sight", recording=scene.recording,
                             start_s=round(max(at - HALF_S, scene.start_s), 2), end_s=round(min(at + HALF_S, scene.end_s), 2),
                             score=round(min(base + (item["score"] - MIN_SCORE) * 0.04, 1.0), 3), attrs=attrs))
    progress(f"scout: looked at {len(wanted)} moments, kept {len(out)} sights")
    return out

"""What is in the way of the camera: its own mount, another camera, a tablet on the panel.

Things close to the lens fill a large part of any view that points their way,
and a model that aims a camera on a panorama underestimates that every time.
So the zones are found once per camera position (a vision model outlines them
on three panoramas taken far apart: what is at the same place in all three is
fixed to the aircraft) and then enforced by arithmetic: a view that would be
partly filled by a zone is turned away from it, narrowed, or refused.

A zone is a box on the sphere: {"what", "yaw": [from, to], "pitch": [from, to]},
in the frame the views are aimed in.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from .ingest import fingerprint

TOO_MUCH = 0.08             # a view may have this share of its picture in a zone
CLEAR = 0.03                # a view that is moved is moved until it is below this
ASPECTS = {"flight": 9 / 16, "holiday": 16 / 9}


def _directions(yaw: float, pitch: float, fov: float, aspect: float) -> tuple[np.ndarray, np.ndarray]:
    """(yaw, pitch) of a grid of points across a normal-lens view (diagonal fov, width / height = aspect)."""
    half = np.tan(np.radians(min(fov, 160.0)) / 2)
    diag = np.hypot(aspect, 1.0)
    x, y = np.meshgrid(np.linspace(-1, 1, 17) * half * aspect / diag, np.linspace(-1, 1, 17) * half / diag)
    z = np.ones_like(x)
    cp, sp = np.cos(np.radians(pitch)), np.sin(np.radians(pitch))
    y2, z2 = y * cp + z * sp, -y * sp + z * cp
    cy, sy = np.cos(np.radians(yaw)), np.sin(np.radians(yaw))
    x3, z3 = x * cy + z2 * sy, -x * sy + z2 * cy
    return np.degrees(np.arctan2(x3, z3)), np.degrees(np.arctan2(y2, np.hypot(x3, z3)))


def share(zones: list[dict], yaw: float, pitch: float, fov: float, aspect: float = 9 / 16) -> float:
    """How much of a view lies inside the zones, 0..1."""
    if not zones:
        return 0.0
    lon, lat = _directions(yaw, pitch, fov, aspect)
    inside = np.zeros(lon.shape, dtype=bool)
    for zone in zones:
        (y0, y1), (p0, p1) = zone["yaw"], zone["pitch"]
        width = (y1 - y0) % 360 or 360.0
        inside |= (((lon - y0) % 360) <= width) & (lat >= min(p0, p1)) & (lat <= max(p0, p1))
    return float(inside.mean())


def clear(zones: list[dict], yaw: float, pitch: float, fov: float, aspect: float = 9 / 16,
          low_fov: float = 60.0) -> tuple[float, float, float] | None:
    """The nearest view that keeps out of the zones: (yaw, pitch, fov), the view itself when it
    already does, None when turning up to 40 degrees and narrowing by a quarter is not enough."""
    if share(zones, yaw, pitch, fov, aspect) <= TOO_MUCH:
        return yaw, pitch, fov
    best = None
    for narrow in (1.0, 0.88, 0.76):
        f = max(fov * narrow, low_fov)
        for dyaw in (0, -8, 8, -16, 16, -24, 24, -32, 32, -40, 40):
            for dpitch in (0, -6, 6, -12, 12, -20):
                p = min(max(pitch + dpitch, -85.0), 60.0)
                if share(zones, yaw + dyaw, p, f, aspect) <= CLEAR:
                    cost = abs(dyaw) + 1.5 * abs(dpitch) + 60 * (1 - narrow)
                    if best is None or cost < best[0]:
                        best = (cost, ((yaw + dyaw + 180) % 360 - 180, p, round(f, 1)))
    return best[1] if best else None


def parse(text: str) -> list[dict]:
    match = re.search(r"\{.*\}", text, re.S)
    try:
        rows = json.loads(match.group(0))["zones"] if match else []
    except (ValueError, KeyError, TypeError):
        rows = []
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            y0, y1 = float(row["yaw"][0]), float(row["yaw"][1])
            p0, p1 = sorted((float(row["pitch"][0]), float(row["pitch"][1])))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if (y1 - y0) % 360 > 200 or p1 - p0 > 120:   # "everything is in the way" is not an answer
            continue
        out.append({"what": str(row.get("what", ""))[:60], "yaw": [round(y0, 1), round(y1, 1)],
                    "pitch": [round(max(p0, -90.0), 1), round(min(p1, 90.0), 1)]})
    return out[:8]


def request_body(model: str, panoramas: list[Path], kind: str) -> dict:
    from . import aiframe
    where = "in a glider's cockpit" if kind == "flight" else "on a selfie stick"
    content = [{"type": "text", "text":
                f"These are three panoramas of the whole 360-degree sphere from one camera {where}, taken minutes "
                "apart, each with a grid of yaw (left to right, -180 to 180) and pitch (0 = horizon, negative = down). "
                "The camera did not move between them, so whatever is fixed to the aircraft is at the same place in "
                "all three, while the world outside differs.\n"
                "List the things that are CLOSE to the camera and would spoil a shot when they fill part of the "
                "frame: the camera's own mount or suction cup, another camera or its holder, a tablet or phone and "
                "its bracket, a strut or tube right in front of the lens. Give each as a box that covers it fully, "
                "with a few degrees to spare.\n"
                "Do NOT list the pilot, the seats, the instrument panel, the canopy frame, the wings or the "
                "propeller: those belong in a film from a cockpit. If nothing is in the way, answer with an empty list.\n"
                "Answer with JSON only: {\"zones\": [{\"what\": \"<a few words>\", \"yaw\": [<from>, <to>], "
                "\"pitch\": [<from>, <to>]}]}  (yaw from the left edge of the thing to its right edge)"}]
    for path in panoramas:
        content.append(aiframe._image(aiframe.pano_sheet(path)))
    return {"model": model, "temperature": 0.1, "messages": [{"role": "user", "content": content}]}


def zones(rec, mount, level, moments: list[float], model: str, kind: str, work: Path, log: Path, folder: Path,
          post) -> list[dict]:
    """The zones of one camera position, asked for once and remembered.
    level: the tilt and nose of that position, or None for a fixed mount; post(body) -> the model's answer."""
    from . import render
    from .contracts import Keyframe
    key = fingerprint(rec.id, [round(float(v)) for v in level] if level else None, [round(m) for m in moments], model,
                      mount.model_dump_json(include={"calibration"}), "zones 1")
    cache = folder / f"zones_{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    panoramas = []
    for at in moments:
        path = folder / f"zonepano_{key}_{round(at)}.jpg"
        if not path.exists():
            render.still(rec, mount, at, Keyframe(t=0, yaw=0, pitch=0, roll=0, fov=100), (1024, 512), path, work, log,
                         level=level, projection="pano")
        panoramas.append(path)
    found = parse(post(request_body(model, panoramas, kind)))
    folder.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    return found


def position_key(level) -> tuple:
    """Scenes filmed from one camera position: the same nose, and a tilt within a few degrees."""
    return ("fixed",) if not level else (round(float(level[2])) if len(level) > 2 else 0,
                                         round(float(level[0]) / 12), round(float(level[1]) / 12))

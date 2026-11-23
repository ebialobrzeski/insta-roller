"""Pick scenes for a reel and split the length budget between them."""
from __future__ import annotations

from .config import Mount, Profile, Theme
from .contracts import ReelPlan, Scene, Shot

MIN_SHOT_S = 2.0


def resolve_framing(names: list[str], scene: Scene, mount: Mount) -> list[str]:
    """*_inner / *_outer follow the circling direction: back_inner -> back_left."""
    direction = scene.attrs.get("direction", "left")
    other = "right" if direction == "left" else "left"
    out = []
    for name in names:
        if name.endswith("_inner"):
            name = f"{name[:-6]}_{direction}"
        elif name.endswith("_outer"):
            name = f"{name[:-6]}_{other}"
        if name not in mount.presets:
            raise KeyError(f"preset '{name}' is not defined in mount '{mount.name}'")
        out.append(name)
    return out


def _window(scene: Scene, length: float, anchor: str) -> float:
    if anchor == "start" or scene.attrs.get("low"):     # low thermal: lowest at entry
        return scene.start_s
    if anchor == "end":
        return scene.end_s - length
    return (scene.start_s + scene.end_s - length) / 2


def plan_reel(scenes: list[Scene], profile: Profile, theme: Theme, mount: Mount,
              target_length_s: float) -> ReelPlan:
    types = profile.scene_types
    pool = [s for s in scenes if s.type in theme.include]

    # The ending is cut from the tail of the last usable scene.
    ending: tuple[Scene, float, float] | None = None
    budget = target_length_s
    if theme.ending and pool:
        last = max(pool, key=lambda s: s.end_s)
        length = min(theme.ending.duration_s, last.end_s - last.start_s)
        ending = (last, last.end_s - length, length)
        budget -= length

    # Best scenes first, while their minimum lengths still fit.
    chosen: list[Scene] = []
    lengths: dict[str, float] = {}
    caps: dict[str, float] = {}
    per_type: dict[str, int] = {}
    for s in sorted(pool, key=lambda s: -s.score * types[s.type].weight):
        room = s.end_s - s.start_s
        if ending and s.id == ending[0].id:
            room -= ending[2]
        need = min(types[s.type].min_shot_s, room)
        if need < MIN_SHOT_S or per_type.get(s.type, 0) >= theme.max_per_type.get(s.type, 10 ** 6):
            continue
        if sum(lengths.values()) + need > budget:
            continue
        chosen.append(s)
        lengths[s.id] = need
        caps[s.id] = min(types[s.type].max_shot_s, room)
        per_type[s.type] = per_type.get(s.type, 0) + 1

    # Share out what is left in proportion to score, up to each scene's cap.
    for _ in range(8):
        spare = budget - sum(lengths.values())
        open_ = [s for s in chosen if caps[s.id] - lengths[s.id] > 0.01]
        if spare < 0.01 or not open_:
            break
        total = sum(s.score * types[s.type].weight for s in open_)
        for s in open_:
            share = spare * s.score * types[s.type].weight / total
            lengths[s.id] = min(lengths[s.id] + share, caps[s.id])

    shots: list[Shot] = []
    for s in sorted(chosen, key=lambda s: s.start_s):
        length = round(lengths[s.id], 2)
        start = _window(s, length, types[s.type].anchor)
        if ending and s.id == ending[0].id:
            start = min(start, ending[1] - length)
        if shots:       # neighbouring scenes can touch; never show the same footage twice
            start = min(max(start, shots[-1].start_s + shots[-1].duration_s), s.end_s - length)
        shots.append(Shot(index=0, scene_id=s.id, type=s.type, start_s=round(max(start, s.start_s), 2),
                          duration_s=length, framing=resolve_framing(types[s.type].framing, s, mount)))
    if ending:
        scene, start, length = ending
        shots.append(Shot(index=0, scene_id=scene.id, type="ending", start_s=round(start, 2),
                          duration_s=round(length, 2),
                          framing=resolve_framing([theme.ending.preset], scene, mount)))
    for i, shot in enumerate(shots):
        shot.index = i + 1
    return ReelPlan(theme=theme.name, target_length_s=target_length_s,
                    length_s=round(sum(s.duration_s for s in shots), 2), shots=shots)

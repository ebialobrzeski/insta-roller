"""Pick scenes for the reels and split each length budget between them.

Themes are templates: which scene types a reel uses and how they are framed.
A scene goes to one reel only, in theme order; a theme with `borrows` (the
flight summary) may reuse scenes of the others.
"""
from __future__ import annotations

from .config import Mount, Profile, Theme
from .contracts import ReelEntry, ReelPlan, Scene, Shot

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
        name = mount.aliases.get(name, name)
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


def _order(s) -> tuple:
    return (s.recording, s.start_s)


def tag(theme: str, length_s: float) -> str:
    return f"{theme}_{length_s:g}s"


def barred_types(themes: list[Theme]) -> set[str]:
    """Scene types gated by a disabled theme (landing off = no circuit, no touchdown)."""
    return {t for th in themes if not th.enabled for t in th.gates}


def plan_reel(scenes: list[Scene], profile: Profile, theme: Theme, mount: Mount,
              target_length_s: float, used: frozenset[str] = frozenset(),
              barred: frozenset[str] = frozenset()) -> ReelPlan:
    """Lengths are output seconds; a sped-up shot uses speed times as much source."""
    types = profile.scene_types
    pool = [s for s in scenes if s.type in theme.include and s.type in types
            and s.type not in barred and (theme.borrows or s.id not in used)]

    def framing(s: Scene) -> list[str]:
        return resolve_framing(theme.framing.get(s.type, types[s.type].framing), s, mount)

    def span(s: Scene) -> float:
        return (s.end_s - s.start_s) / types[s.type].speed

    # The ending is cut from the tail of the last usable scene; a recorded landing ends the reel itself.
    ending: tuple[Scene, float, float] | None = None
    budget = target_length_s
    if theme.ending and pool and not any(s.type == "landing" for s in pool):
        last = max(pool, key=_order)
        length = min(theme.ending.duration_s, last.end_s - last.start_s)
        ending = (last, last.end_s - length, length)
        budget -= length

    # Best scenes first, while their minimum lengths still fit.
    chosen: list[Scene] = []
    lengths: dict[str, float] = {}
    caps: dict[str, float] = {}
    per_type: dict[str, int] = {}
    for s in sorted(pool, key=lambda s: -s.score * types[s.type].weight):
        room = span(s)
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
        total = sum(s.score * types[s.type].weight for s in open_) or 1.0
        for s in open_:
            share = spare * s.score * types[s.type].weight / total
            lengths[s.id] = min(lengths[s.id] + share, caps[s.id])

    shots: list[Shot] = []
    for s in sorted(chosen, key=_order):
        speed = types[s.type].speed
        length = round(lengths[s.id] * speed, 2)                 # source seconds
        start = _window(s, length, types[s.type].anchor)
        if ending and s.id == ending[0].id:
            start = min(start, ending[1] - length)
        if shots and shots[-1].recording == s.recording:        # never show the same footage twice
            start = min(max(start, shots[-1].start_s + shots[-1].duration_s), s.end_s - length)
        shots.append(Shot(index=0, scene_id=s.id, type=s.type, start_s=round(max(start, s.start_s), 2),
                          duration_s=length, framing=framing(s), recording=s.recording, speed=speed,
                          attrs={k: v for k, v in s.attrs.items() if k.startswith(("yaw_", "track_"))}))
    if ending:
        scene, start, length = ending
        shots.append(Shot(index=0, scene_id=scene.id, type="ending", start_s=round(start, 2),
                          duration_s=round(length, 2), recording=scene.recording,
                          framing=resolve_framing([theme.ending.preset], scene, mount)))
    for i, shot in enumerate(shots):
        shot.index = i + 1
    return ReelPlan(theme=theme.name, title=theme.title, target_length_s=target_length_s,
                    length_s=round(sum(s.duration_s / s.speed for s in shots), 2), shots=shots)


def material_s(scenes: list[Scene], profile: Profile, theme: Theme, used: frozenset[str],
               barred: frozenset[str]) -> float:
    """Footage a theme could show: every free scene, up to its longest shot."""
    types = profile.scene_types
    return sum(min((s.end_s - s.start_s) / types[s.type].speed, types[s.type].max_shot_s) for s in scenes
               if s.type in theme.include and s.type in types and s.type not in barred
               and (theme.borrows or s.id not in used))


def plan_reels(scenes: list[Scene], profile: Profile, themes: list[Theme], mount: Mount,
               length_s: float | None = None) -> tuple[list[ReelPlan], list[ReelEntry]]:
    """One reel per enabled theme with enough material. length_s overrides every theme's own length."""
    barred = frozenset(barred_types(themes))
    used: set[str] = set()
    plans: list[ReelPlan] = []
    entries: list[ReelEntry] = []
    for theme in sorted(themes, key=lambda t: t.borrows):      # borrowers choose last
        target = length_s or theme.target_length_s or profile.target_length_s
        entry = ReelEntry(theme=theme.name, tag=tag(theme.name, target), status="skipped")
        entries.append(entry)
        if not theme.enabled:
            entry.reason = "theme is switched off"
            continue
        have = material_s(scenes, profile, theme, frozenset(used), barred)
        if have < max(theme.min_material_s, MIN_SHOT_S):
            entry.reason = f"not enough material ({have:.0f}s of {theme.min_material_s:g}s needed)"
            continue
        plan = plan_reel(scenes, profile, theme, mount, target, frozenset(used), barred)
        if not plan.shots:
            entry.reason = "no scene fits the length"
            continue
        if not theme.borrows:
            used.update(s.scene_id for s in plan.shots)
        entry.status, entry.length_s, entry.shots = "planned", plan.length_s, len(plan.shots)
        plans.append(plan)
    order = [t.name for t in themes]
    plans.sort(key=lambda p: order.index(p.theme))
    entries.sort(key=lambda e: order.index(e.theme))
    return plans, entries

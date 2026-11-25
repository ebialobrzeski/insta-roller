"""Profile "urlop": walks with a selfie stick, many short clips over several days.

Everything here works from the camera's sensors and sound, without looking
at the picture:

- the gravity sensor tells how the stick is held; a lowered camera is dropped
  and the rest is levelled scene by scene,
- the gyroscope tells walking from standing still (panorama),
- the sound level marks speech (the holder talks to the camera),
- the clip time gives the day, with the boundary moved to the early morning.

Directions: the holder faces the screen-side lens (the camera's second video
track), so the view ahead is the other lens. A stick tilted clearly the other
way flips the two. Without GPS this is the best guess for the walking direction.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from . import audio, contracts
from .config import Holiday
from .contracts import ReelEntry, ReelPlan, RecordingInfo, Scene, Scenes, Sync
from .ingest import Recording, fingerprint
from .reel import material_s, plan_reel, tag
from .telemetry import CameraData

UPRIGHT = np.array([-1.0, 0.0, 0.0])     # accelerometer reading of a camera standing upright


def clip_level(cam: CameraData, start_s: float = 0.0, end_s: float = 1e9) -> tuple[float, float] | None:
    """(pitch, roll) for v360 (rorder rpy) that levels the picture, from the mean gravity direction."""
    if not cam.has_imu:
        return None
    part = cam.imu[(cam.imu[:, 0] >= start_s) & (cam.imu[:, 0] < end_s)]
    if not len(part):
        part = cam.imu
    return level_of(part[:, 1:4].mean(0))


def level_of(up: np.ndarray) -> tuple[float, float]:
    """(pitch, roll) from the accelerometer's up direction. Camera axes: x along
    the body (down), y through the lenses, z sideways. Checked on real clips."""
    x, y, z = up / max(float(np.linalg.norm(up)), 1e-6)
    return (round(float(-np.degrees(np.arctan2(y, np.hypot(x, z)))), 1),
            round(float(np.degrees(np.arctan2(z, -x))), 1))


def day_of(rec_id: str, cfg: Holiday) -> str:
    """Local day of a clip; the camera names files by local time. Clips from
    before day_starts_at_h belong to the evening before."""
    try:
        local = datetime.strptime(rec_id[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return "unknown"
    return (local - timedelta(hours=cfg.day_starts_at_h)).strftime("%Y%m%d")


def clip_scenes(rec: Recording, cam: CameraData, level_db: np.ndarray, cfg: Holiday, base: dict[str, float],
                day: str) -> list[Scene]:
    """Scenes of one clip: view (walking), walk (long walking, sped up), holder (speech), panorama (standing)."""
    start, end = cfg.trim_s, rec.duration_s - cfg.trim_s
    if end - start < cfg.window_s or not cam.has_imu:
        return []
    floor = float(np.percentile(level_db, 20)) if len(level_db) else 0.0
    windows = []            # (t0, t1, kind, up)
    t = start
    while t + cfg.window_s <= end + 1e-6:
        part = cam.imu[(cam.imu[:, 0] >= t) & (cam.imu[:, 0] < t + cfg.window_s)]
        if len(part) < 5:
            t += cfg.window_s
            continue
        up = part[:, 1:4].mean(0)
        up /= max(np.linalg.norm(up), 1e-6)
        tilt = float(np.degrees(np.arccos(np.clip(up @ UPRIGHT, -1, 1))))
        turning = float(np.sqrt((part[:, 4:7] ** 2).sum(1).mean()))
        loud = len(level_db) > int(t) and float(level_db[int(t):int(t + cfg.window_s) + 1].mean()) > floor + cfg.speech_db
        kind = "lowered" if tilt > cfg.lowered_tilt_deg else "holder" if loud else \
            "panorama" if turning < cfg.still_dps else "view"
        windows.append((t, t + cfg.window_s, kind, up))
        t += cfg.window_s

    runs: list[list] = []   # [t0, t1, kind, [ups]]
    for t0, t1, kind, up in windows:
        if runs and runs[-1][2] == kind:
            runs[-1][1] = t1
            runs[-1][3].append(up)
        else:
            runs.append([t0, t1, kind, [up]])
    merged: list[list] = []
    for run in runs:                        # a short stop is part of the walk around it
        if run[2] == "panorama" and run[1] - run[0] < cfg.still_min_s:
            run[2] = "view"
        if merged and merged[-1][2] == run[2]:
            merged[-1][1] = run[1]
            merged[-1][3] += run[3]
        else:
            merged.append(run)
    scenes = []
    for i, (t0, t1, kind, ups) in enumerate(merged):
        if kind == "lowered":
            continue
        if kind == "view" and t1 - t0 >= cfg.long_walk_s:
            kind = "walk"
        if kind not in base:
            continue
        up = np.mean(ups, axis=0)
        up /= max(np.linalg.norm(up), 1e-6)
        holder = 0.0 if up[1] > 0.35 else 180.0        # which lens the holder faces
        level = level_of(up)
        scenes.append(Scene(id=f"{rec.id}_{i:02d}_{kind}", type=kind, start_s=round(t0, 2), end_s=round(t1, 2),
                            score=round(base[kind], 3), recording=rec.id,
                            attrs={"day": day, "level": list(level), "yaw_holder": holder,
                                   "yaw_travel": (holder + 180.0) % 360}))
    return scenes


def analyze(job) -> Scenes:
    profile, cfg = job.profile, job.profile.holiday
    recordings = job.recordings
    fp = fingerprint(*[f for r in recordings for f in r.files], cfg.model_dump_json(),
                     {k: v.base_score for k, v in profile.scene_types.items()}, sorted(profile.people),
                     profile.ai_framing.model_dump_json(include={"scout", "scout_every_holiday_s", "scout_max", "model"})
                     if profile.ai_framing.scout and profile.ai_framing.engine == "openrouter" else "", 3)
    path = job.path("scenes.json")
    if path.exists() and not job.args.force:
        cached = contracts.read(path, Scenes)
        if cached.fingerprint == fp:
            print(f"analyze: cached ({len(cached.scenes)} scenes)")
            return cached
    base = {k: v.base_score for k, v in profile.scene_types.items()}
    scenes, infos, lines = [], [], []
    for number, rec in enumerate(recordings, 1):
        print(f"  shot {number}/{len(recordings)}  clip {rec.id}", flush=True)       # progress, as render prints it
        cam = job.sensors(rec)
        day = day_of(rec.id, cfg)
        level = np.concatenate([audio.level_db(f) for f in rec.best().files] or [np.zeros(0)])
        found = clip_scenes(rec, cam, level, cfg, base, day)
        scenes += found
        infos.append(RecordingInfo(id=rec.id, kind=rec.kind, files=[f.name for f in rec.files],
                                   duration_s=round(rec.duration_s, 2), start_utc=rec.start_utc, day=day))
        kinds = ", ".join(f"{s.type} {s.end_s - s.start_s:.0f}s" for s in found) or "nothing usable"
        lines.append(f"  {day}  {rec.id}  {rec.duration_s:>6.1f}s  {kinds}")
    annotate_people(job, {r.id: r for r in recordings}, scenes)
    if profile.ai_framing.scout and profile.ai_framing.engine == "openrouter" and scenes:
        from . import aiframe, scout                # a model looks through the clips for landmarks and views
        print("analyze: looking through the clips for landmarks", flush=True)
        try:
            sights = scout.scout_clips({r.id: r for r in recordings}, job.mount, profile, scenes, job.work, job.log)
            scenes = sorted(scenes + sights, key=lambda s: (s.recording, s.start_s))
        except (aiframe.Unavailable, RuntimeError) as error:
            print(f"scout: {error}; no landmarks were looked for")
    result = Scenes(fingerprint=fp, recording=infos[0], recordings=infos, source="holiday",
                    sync=Sync(video_start_utc=recordings[0].start_utc or 0.0, offset_s=0.0, method="camera clock"),
                    missing_phases=[], scenes=scenes)
    contracts.write(path, result)
    days = sorted({i.day for i in infos})
    report = "\n".join([f"{len(recordings)} clips on {len(days)} days, {len(scenes)} scenes", ""] + lines)
    job.path("scenes_report.txt").write_text(report + "\n", encoding="utf-8")
    print(f"analyze: {len(scenes)} scenes -> {path}")
    print(report)
    job.note("analyze", fingerprint=fp, scenes=len(scenes), source="holiday")
    return result


def plan(job, scenes: Scenes) -> tuple[list[ReelPlan], list[ReelEntry]]:
    """Mode day: one film per day. Mode trip: one film of everything, the days in
    order, each with a share of the length that matches its good material."""
    profile, cfg, mount = job.profile, job.profile.holiday, job.mount
    theme = job.themes[0]
    by_day: dict[str, list[Scene]] = {}
    for s in scenes.scenes:
        by_day.setdefault(s.attrs.get("day", "unknown"), []).append(s)
    # A film takes few shots of the person holding the camera. The other scenes
    # that found only the holder are shown as the view ahead instead of dropped.
    limit = theme.max_per_type.get("holder")
    if limit is not None and "view" in profile.scene_types:
        for day, items in by_day.items():
            keep = {s.id for s in sorted((s for s in items if s.type == "holder"), key=lambda s: -s.score)[:limit]}
            by_day[day] = [s.model_copy(update={"type": "view", "score": profile.scene_types["view"].base_score})
                           if s.type == "holder" and s.id not in keep else s for s in items]
    plans, entries = [], []
    none = frozenset()

    def add(name: str, target: float, shots_plan: ReelPlan | None, reason: str = "") -> None:
        entry = ReelEntry(theme=name, tag=tag(name, target), status="skipped", reason=reason)
        if shots_plan and shots_plan.shots:
            shots_plan.theme = name
            entry.status, entry.length_s, entry.shots = "planned", shots_plan.length_s, len(shots_plan.shots)
            plans.append(shots_plan)
        entries.append(entry)

    if job.args.mode in ("day", "both"):
        target = job.length or theme.target_length_s or profile.target_length_s
        for day in sorted(by_day):
            have = material_s(by_day[day], profile, theme, none, none)
            if have < max(theme.min_material_s, 2.0):
                add(f"day_{day}", target, None, f"not enough material ({have:.0f}s)")
            else:
                add(f"day_{day}", target, plan_reel(by_day[day], profile, theme, mount, min(target, have)))
    if job.args.mode in ("trip", "both"):
        target = job.length if job.args.mode == "trip" and job.length else cfg.trip_length_s
        have = {d: material_s(s, profile, theme, none, none) for d, s in by_day.items()}
        have = {d: h for d, h in have.items() if h >= 2.0}
        total = sum(have.values()) or 1.0
        share = {d: max(target * h / total, min(cfg.min_per_day_s, h)) for d, h in have.items()}
        scale = min(target / (sum(share.values()) or 1.0), 1.0)
        shots = []
        for day in sorted(have):
            shots += plan_reel(by_day[day], profile, theme, mount, min(share[day] * scale, have[day])).shots
        for i, shot in enumerate(shots):
            shot.index = i + 1
        trip = ReelPlan(theme="trip", title="Wyjazd", target_length_s=target,
                        length_s=round(sum(s.duration_s / s.speed for s in shots), 2), shots=shots)
        add("trip", target, trip, "no usable material")
    everything = [s for day in sorted(by_day) for s in by_day[day]]
    for extra in job.themes[1:]:                # further themes: one film each of the whole trip
        if not extra.enabled:
            continue
        target = extra.target_length_s or profile.target_length_s
        have = material_s(everything, profile, extra, none, none)
        if have < max(extra.min_material_s, 2.0):
            add(extra.name, target, None, f"not enough material ({have:.0f}s of {extra.min_material_s:g}s needed)")
        else:
            add(extra.name, target, plan_reel(everything, profile, extra, mount, min(target, have)))
            plans[-1].title = extra.title
    return plans, entries


def levels(job, scenes: Scenes | None) -> dict[str, tuple[float, float]]:
    """Scene id -> (pitch, roll) that levels its picture."""
    if scenes is None:
        return {}
    return {s.id: tuple(s.attrs["level"]) for s in scenes.scenes if "level" in s.attrs}


def people_file(job):
    """people.json made by `insta-roller faces-export`: in the job folder, the output folder or the repo."""
    from .config import PROFILES_DIR
    for folder in (job.folder, job.out, PROFILES_DIR.parent):
        if (folder / "people.json").exists():
            return folder / "people.json"
    return None


def annotate_people(job, recordings: dict[str, Recording], scenes: list[Scene]) -> None:
    """Known people raise a scene's score and give the `companion` framing a target.
    Strangers are never a target. Without people.json or the face model nothing changes."""
    path = people_file(job)
    if path is None:
        return
    try:
        from . import faces
        people = faces.try_load(path, faces.DEFAULT_MODEL)
        if not people or not faces.available():
            print("people: face model not installed or people.json unusable; films are made without people framing")
            return
        names = [n.strip() for n in (job.args.people or "").split(",") if n.strip()]
        weights = {n: job.profile.people.get(n, 1.0) for n in names} if names else dict(job.profile.people)
        types = job.profile.scene_types
        shown: dict[str, float] = {}
        print(f"people: looking for {len(weights)} known people in {len(scenes)} scenes", flush=True)
        for number, scene in enumerate(scenes, 1):
            print(f"  shot {number}/{len(scenes)}  faces {scene.id}", flush=True)
            level = tuple(scene.attrs["level"]) if "level" in scene.attrs else None
            span = scene.end_s - scene.start_s
            times = sorted({round(scene.start_s + min(1.0, span / 4), 2), round(scene.start_s + span / 2, 2),
                            round(scene.end_s - min(1.0, span / 4), 2)})
            rec = recordings[scene.recording].best()
            seen = faces.scan((v for t in times for v in _views(rec, job.mount, t, level, job.work, job.log)), people)
            middle = times[len(times) // 2]

            def near_holder(sighting) -> bool:
                return abs((sighting.yaw - scene.attrs["yaw_holder"] + 180) % 360 - 180) < 45

            # The person with the stick is the largest face on the side the holder stands.
            close = [s for s in seen if s.name and near_holder(s)]
            holder = max(close, key=lambda s: s.size).name if close else None
            others = [s for s in seen if s.name != holder]
            target = faces.choose_target([s for s in others if s.t == middle], weights, shown)
            target = target or faces.choose_target(others, weights, shown)
            kind = "person"
            if target is None:                  # nobody else: a shot of the holder, of which a film takes few
                target = faces.choose_target([s for s in seen if s.name == holder], weights, shown)
                kind = "holder"
            if target is None:
                continue
            scene.score = round(min(scene.score + faces.scene_bonus(seen, job.profile.person_bonus, weights), 1.0), 3)
            scene.attrs["people"] = list(target.names)
            track = []
            for t in times:                     # where the same people are at each moment
                now = [s for s in seen if s.t == t and s.name in target.names]
                if now:
                    yaws = np.radians([s.yaw for s in now])
                    track.append([t, round(float(np.degrees(np.arctan2(np.sin(yaws).mean(), np.cos(yaws).mean()))), 1),
                                  round(float(np.mean([s.pitch for s in now])), 1)])
            if len(track) < len(times):         # seen only in passing: a better score, but no framing on them
                continue
            mid = track[len(track) // 2]
            scene.attrs[f"yaw_{kind}"], scene.attrs[f"pitch_{kind}"] = mid[1], mid[2]
            if len(track) > 1:
                scene.attrs[f"track_{kind}"] = track
            wanted = "companion" if kind == "person" else "holder"
            if scene.type in ("view", "walk") and wanted in types:
                scene.type = wanted
            length = min(span, types[scene.type].max_shot_s)
            shown = faces.screen_time(shown, target, length)
    except Exception as error:          # recognition must never stop the films
        print(f"people: skipped ({error})")


SCAN_YAWS, SCAN_FOV, SCAN_SIZE = (0, 90, 180, -90), 110.0, 800


def _views(rec: Recording, mount, t: float, level, work, log):
    """Four flat stills around the horizon at one moment, from a single decode: (t, image, yaw, pitch, fov)."""
    from . import ffmpeg
    from .ingest import EQ
    folder = work / "faces"
    folder.mkdir(parents=True, exist_ok=True)
    outs = [folder / f"{rec.id}_{t:.2f}_{yaw}.jpg" for yaw in SCAN_YAWS]
    if not all(o.exists() for o in outs):
        graph = f"{rec.source_graph(mount, level)};{EQ}split={len(outs)}" + "".join(f"[e{i}]" for i in range(len(outs)))
        maps = []
        for i, yaw in enumerate(SCAN_YAWS):
            graph += (f";[e{i}]v360=input=e:output=flat:w={SCAN_SIZE}:h={SCAN_SIZE}:d_fov={SCAN_FOV:g}"
                      f":yaw={yaw}:pitch=0[v{i}]")
            maps += ["-map", f"[v{i}]", "-frames:v", "1", "-q:v", "3", str(outs[i])]
        try:                                    # the nearest keyframe, decoded on the graphics card
            ffmpeg.run([*rec.input_args(work, t, still=True), "-filter_complex", graph, *maps], log=log)
        except RuntimeError:
            ffmpeg.run([*rec.input_args(work, t), "-filter_complex", graph, *maps], log=log)
    for out, yaw in zip(outs, SCAN_YAWS):
        yield t, str(out), float(yaw), 0.0, SCAN_FOV

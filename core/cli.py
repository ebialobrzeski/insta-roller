"""Command line: every pipeline step is its own idempotent command."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import audio, contracts, render, stitch, telemetry
from . import sync as syncer
from .config import Theme, list_names, load_mount, load_profile, load_theme
from .contracts import FramingPlan, JobState, RecordingInfo, ReelPlan, ReelSet, Scenes, Sync, Telemetry
from .framing import plan_framing
from .igc import Fix, parse_igc
from .ingest import Recording, discover, fingerprint
from .phases import build_track, detect_phases, phases_from_gyro
from .reel import plan_reels
from .scenes import build_scenes, split_long, telemetry_samples


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


def guess_profile(folder: Path) -> str:
    """A flight log in the folder means a flight; anything else is a holiday."""
    return "flight" if any(p.suffix.lower() == ".igc" for p in Path(folder).iterdir() if p.is_file()) else "holiday"


class Job:
    def __init__(self, args):
        self.args = args
        self.folder = Path(args.folder).resolve()
        self.out = Path(args.out).resolve() if args.out else self.folder / "insta-roller"
        self.work = Path(args.work).resolve() if args.work else self.out / ".cache"
        self.log = self.out / "logs" / "ffmpeg.log"
        name = args.profile if args.profile != "auto" else guess_profile(self.folder)
        self.profile = load_profile(name)
        if args.overlay is not None:
            self.profile.overlay.enabled = args.overlay
        if getattr(args, "ai_framing", None):
            self.profile.ai_framing.engine = args.ai_framing
        if getattr(args, "ai_model", None):
            self.profile.ai_framing.model = args.ai_model
        if getattr(args, "ai_reasoning", None) is not None:
            self.profile.ai_framing.reasoning_tokens = max(args.ai_reasoning, 0)
        if getattr(args, "ai_scout", None) is not None:
            self.profile.ai_framing.scout = args.ai_scout
        self.mount = load_mount(args.mount or self.profile.mount)
        self.length = args.length
        self._recordings: list[Recording] | None = None
        self._recording: Recording | None = None
        self._igc: Path | None = None

    @property
    def themes(self) -> list[Theme]:
        names = self.args.themes.split(",") if self.args.themes else self.profile.themes
        themes = [load_theme(n.strip()) for n in names if n.strip()]
        lengths = dict(item.split("=", 1) for item in (getattr(self.args, "lengths", None) or "").split(",") if "=" in item)
        counts = dict(item.split("=", 1) for item in (getattr(self.args, "counts", None) or "").split(",") if "=" in item)
        for theme in themes:
            if theme.name in lengths:               # this job's own length for the reel
                theme.target_length_s = float(lengths[theme.name])
            if theme.name in counts:                # ... and how many reels of it
                theme.count = min(max(int(counts[theme.name]), 1), 12)
            if theme.name in (self.args.enable or "").split(","):
                theme.enabled = True
            if theme.name in (self.args.disable or "").split(","):
                theme.enabled = False
        return themes

    # --- inputs -----------------------------------------------------------
    @property
    def recordings(self) -> list[Recording]:
        """Every take in the folder."""
        if self._recordings is None:
            recordings, self._igcs = discover(self.folder)
            if not recordings:
                sys.exit(f"No camera files (.insv, .lrv) or equirectangular .mp4 in {self.folder}")
            self._recordings = recordings
        return self._recordings

    def _pick(self) -> None:
        recordings = self.recordings
        igcs = [] if self.args.no_igc else self._igcs
        if self.args.igc:
            igcs = [Path(self.args.igc).resolve()]
        if self.args.recording:
            match = [r for r in recordings if r.id == self.args.recording]
            if not match:
                sys.exit(f"Recording '{self.args.recording}' not found. Available: "
                         + ", ".join(r.id for r in recordings))
            recordings = match
        self._recording = max(recordings, key=lambda r: r.duration_s)
        self._igc = igcs[0] if len(igcs) == 1 and len(recordings) == 1 else None

        # Several logs or takes: pair up the log and the take that overlap most.
        best = 0.0
        for igc in igcs:
            fixes = parse_igc(igc)
            for rec in recordings:
                if rec.start_utc is None:
                    continue
                shared = min(rec.start_utc + rec.duration_s, fixes[-1].t) - max(rec.start_utc, fixes[0].t)
                if shared > best:
                    best, self._igc, self._recording = shared, igc, rec

    @property
    def recording(self) -> Recording:
        if self._recording is None:
            self._pick()
        return self._recording

    @property
    def igc(self) -> Path | None:
        if self._recording is None:
            self._pick()
        return self._igc

    def sensors(self, rec: Recording) -> telemetry.CameraData:
        """Gyroscope and GPS of a take, from whichever of its files carries them."""
        for candidate in (rec if rec.kind != "equirect" else None, rec.alt.get("dual_fisheye"), rec.alt.get("insv")):
            if candidate is not None:
                data = telemetry.read_recording(candidate.files, candidate.durations)
                if data.has_imu or data.has_gps:
                    return data
        return telemetry.read_recording([], [])

    # --- outputs ----------------------------------------------------------
    def path(self, name: str) -> Path:
        return self.out / name

    def note(self, step: str, **info) -> None:
        state_path = self.path("job_state.json")
        state = contracts.read(state_path, JobState) if state_path.exists() else JobState()
        state.steps[step] = {"at": datetime.now().isoformat(timespec="seconds"), **info}
        for value in info.get("outputs", []):
            if value not in state.outputs:
                state.outputs.append(value)
        contracts.write(state_path, state)

    def context(self, scenes: Scenes | None = None) -> render.Context:
        tele_path = self.path("telemetry.json")
        tele = contracts.read(tele_path, Telemetry) if tele_path.exists() else None
        recordings = {r.id: r for r in self.recordings}
        if self.profile.kind == "flight":
            recordings[""] = self.recording
        else:
            recordings[""] = self.recordings[0]
        stitcher = None
        exe = stitch.find_exe()
        if self.args.source == "sdk" and exe is None:
            sys.exit("--source sdk needs the stitcher: build stitcher/ or set INSTA_ROLLER_STITCHER.")
        if exe is not None and self.args.source in ("auto", "sdk"):
            stitcher = stitch.Stitcher(exe, self.work / "stitched", self.args.cache_gb,
                                       local_sources=self.args.copy_local)
        from . import holiday                   # scene id -> the tilt (and nose) found for it, any kind of job
        levels = holiday.levels(self, scenes) or None
        return render.Context(recordings, self.mount, self.profile, self.work, self.log, tele, levels,
                              stitcher, "proxy" if self.args.source == "proxy" else "auto")


def _engine_off(job: Job, rec: Recording, start_utc: float) -> float | None:
    """Motor glider: UTC at which the engine sound stops."""
    d = job.profile.detection
    levels = [audio.level_db(f) for f in rec.files]
    level = np.concatenate(levels) if levels else np.zeros(0)
    second = audio.engine_off(level, d.engine_off_drop_db, d.engine_off_hold_s, d.release_min_after_takeoff_s)
    return None if second is None else start_utc + second


def step_analyze(job: Job) -> Scenes:
    if job.profile.kind != "flight":
        from . import holiday
        return holiday.analyze(job)
    rec, igc, profile = job.recording, job.igc, job.profile
    fp = fingerprint(igc or "no igc", *rec.files, profile.detection.model_dump_json(), profile.aircraft,
                     {k: v.base_score for k, v in profile.scene_types.items()},
                     profile.thermal_good_climb_ms, profile.thermal_low_agl_m, profile.thermal_low_bonus,
                     profile.sync.model_dump_json(), job.args.offset, job.args.video_start,
                     job.mount.model_dump_json(include={"calibration"}),
                     profile.ai_framing.model_dump_json(include={"scout", "scout_every_s", "scout_max", "scout_apart_s", "model"})
                     if profile.ai_framing.scout and profile.ai_framing.engine == "openrouter" else "", 5)
    path = job.path("scenes.json")
    if path.exists() and job.path("telemetry.json").exists() and not job.args.force:
        cached = contracts.read(path, Scenes)
        if cached.fingerprint == fp:
            print(f"analyze: cached ({len(cached.scenes)} scenes)")
            return cached

    print("analyze: reading the camera's sensors", flush=True)
    cam = job.sensors(rec)
    fixes, source = None, "gyro"
    if igc is not None:
        fixes, source = parse_igc(igc), "igc"
    elif cam.has_gps:
        g = cam.gps[::10]                       # 10 Hz -> 1 Hz
        fixes, source = [Fix(float(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in g], "camera_gps"

    # Video clock -> UTC.
    print("analyze: matching the video to the flight log", flush=True)
    result, correlation = None, None
    if job.args.video_start:
        base = datetime.fromisoformat(job.args.video_start.replace("Z", "+00:00"))
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        start, method = base.timestamp(), "manual start time"
    else:
        track = None
        if fixes:
            track = (np.array([f.t for f in fixes]), np.array([f.lat for f in fixes]), np.array([f.lon for f in fixes]))
        result = syncer.synchronise(cam, rec.duration_s, rec.start_utc, track, source == "camera_gps",
                                    profile.sync.search_s, profile.sync.min_correlation)
        if result is None:
            sys.exit("The recording has no creation time. Pass --video-start (UTC of the first frame).")
        start, method, correlation = result.video_start_utc, result.method, result.correlation
    start += job.args.offset
    if job.args.offset:
        method += " + manual offset"

    print("analyze: finding the phases of the flight", flush=True)
    samples = []
    if fixes:
        track_ = build_track(fixes, profile.detection)
        engine_off = _engine_off(job, rec, start) if profile.aircraft == "motorglider" else None
        phases = detect_phases(fixes, profile.detection, profile.aircraft, engine_off, track_)
        samples = telemetry_samples(track_, start, rec.duration_s)
        if cam.has_imu:                         # the gyroscope follows the bank without the GPS lag
            turn = np.convolve(telemetry.turn_about_vertical(cam.imu), np.ones(100) / 100, "same")
            rate = np.interp([s.t for s in samples], cam.imu[:, 0], turn)
            for s, r in zip(samples, rate):
                s.bank_deg = round(float(np.degrees(np.arcsin(np.clip(
                    s.speed_kmh / 3.6 * np.radians(r) / 9.81, -0.94, 0.94)))), 1)
        span = f"{_utc(fixes[0].t)} - {_utc(fixes[-1].t)} UTC"
    elif cam.has_imu:
        seconds = np.arange(0, int(rec.duration_s))
        turn = np.interp(seconds, cam.imu[:, 0], telemetry.turn_about_vertical(cam.imu))
        phases = phases_from_gyro([float(x) for x in turn], start, profile.detection)
        span = "gyroscope only"
    else:
        sys.exit(f"No IGC file, no GPS and no gyroscope data for {rec.id}: nothing to find scenes with.")
    home = None
    if fixes and samples:                       # heading and position, for views held on the world
        from . import heading as heading_
        ft = np.array([f.t for f in fixes])
        flat, flon = np.array([f.lat for f in fixes]), np.array([f.lon for f in fixes])
        at = np.array([x.t for x in samples]) + start
        gyro = (cam.imu[:, 0] + start, telemetry.turn_about_vertical(cam.imu)) if cam.has_imu else (None, None)
        where = heading_.headings(ft, flat, flon, at, *gyro)
        for x, h, la, lo in zip(samples, where, np.interp(at, ft, flat), np.interp(at, ft, flon)):
            x.heading_deg, x.lat, x.lon = round(float(h) % 360, 1), round(float(la), 6), round(float(lo), 6)
        home = [fixes[0].lat, fixes[0].lon, fixes[0].alt]
    scenes, missing = build_scenes(phases, start, rec.duration_s, profile)
    scenes = split_long(scenes, profile.detection.split_long_s)
    cal = rec.calibration(job.mount)
    if cal is not None and cal.auto_nose and cam.has_imu:       # a camera that is not in a fixed place
        from . import mountcal
        speed = np.array([[x.t, x.speed_kmh / 3.6] for x in samples]) if samples else None
        found = mountcal.calibrate(cam.imu, [(x.start_s, x.end_s) for x in scenes], speed)
        known = [(x, f) for x, f in zip(scenes, found) if f is not None]
        for scene, f in zip(scenes, found):                     # a scene the sensors say nothing about takes its neighbour's
            if f is None and known:
                f = min(known, key=lambda k: abs(k[0].start_s - scene.start_s))[1]
            if f is not None:
                scene.attrs["level"] = [float(v) for v in f]
        print(f"analyze: camera position found from its sensors for {len(known)} of {len(scenes)} scenes"
              + ("" if known else "; the mount's fixed angles are used"))
    if profile.ai_framing.scout and profile.ai_framing.engine == "openrouter" and scenes:
        from . import aiframe, scout                # a model looks through the whole flight for sights
        print("analyze: looking through the flight for sights", flush=True)
        try:
            scenes = sorted(scenes + scout.scout(rec, job.mount, profile, scenes, rec.duration_s, job.work, job.log),
                            key=lambda x: x.start_s)
        except (aiframe.Unavailable, RuntimeError) as error:
            print(f"scout: {error}; no sights were looked for")
    info = RecordingInfo(id=rec.id, kind=rec.kind, files=[f.name for f in rec.files],
                         duration_s=round(rec.duration_s, 2), start_utc=start)
    result = Scenes(fingerprint=fp, recording=info, recordings=[info], source=source,
                    sync=Sync(video_start_utc=start, offset_s=job.args.offset, method=method, correlation=correlation),
                    missing_phases=missing, scenes=scenes)
    contracts.write(path, result)
    contracts.write(job.path("telemetry.json"), Telemetry(source=source, samples=samples, home=home))

    names = {"igc": f"IGC {igc.name}" if igc else "", "camera_gps": "GPS remote data in the camera file",
             "gyro": "no IGC and no GPS"}
    corr = f", correlation {correlation:.2f}" if correlation else ""
    lines = [f"Recording {rec.id} ({rec.kind}), {_hms(rec.duration_s)}, starts {_utc(start)} UTC ({method}{corr})",
             f"Scenes from: {names[source]}: {span}, {len(phases)} phases", ""]
    for s in scenes:
        attrs = ", ".join(f"{k}={v}" for k, v in s.attrs.items())
        lines.append(f"  {_hms(s.start_s)} - {_hms(s.end_s)}  {s.type:<10} score {s.score:.2f}  {attrs}")
    if missing:
        lines.append(f"\nNot covered by the recording: {', '.join(missing)}")
    report = "\n".join(lines)
    job.path("scenes_report.txt").write_text(report + "\n", encoding="utf-8")
    print(f"analyze: {len(scenes)} scenes -> {path}")
    print(report)
    job.note("analyze", fingerprint=fp, scenes=len(scenes), source=source, sync=method)
    return result


def step_plan(job: Job) -> list[FramingPlan]:
    scenes_path = job.path("scenes.json")
    if not scenes_path.exists():
        sys.exit("No scenes.json yet. Run `analyze` first.")
    scenes = contracts.read(scenes_path, Scenes)
    if job.profile.kind != "flight":
        from . import holiday
        plans, entries = holiday.plan(job, scenes)
    else:
        takes = {}                                  # a reel asked to be made anew keeps its take number in its plan
        for path in job.out.glob("reel_plan_*.json"):
            try:
                old = contracts.read(path, ReelPlan)
                takes[old.theme] = old.take
            except Exception:
                pass
        plans, entries = plan_reels(scenes.scenes, job.profile, job.themes, job.mount, job.length, takes)
    tele_path = job.path("telemetry.json")
    tele = contracts.read(tele_path, Telemetry) if tele_path.exists() else None
    calibration = job.recordings[0].calibration(job.mount) if job.profile.kind != "flight" \
        else job.recording.best().calibration(job.mount)
    if job.args.source in ("auto", "sdk") and stitch.find_exe() is not None and "sdk" in job.mount.calibration:
        calibration = job.mount.calibration["sdk"]        # the Media SDK stitch will be the source

    framings = []
    ai_ctx = job.context(scenes) if job.profile.ai_framing.engine != "off" else None
    themes = {t.name: t for t in job.themes}
    shown: list[str] = []                       # what the reels planned so far show, for the AI
    for reel, entry in zip(plans, [e for e in entries if e.status == "planned"]):
        reel_path = job.path(f"reel_plan_{entry.tag}.json")
        framing_path = job.path(f"framing_plan_{entry.tag}.json")
        # A reel plan edited by hand (or in the scene review) is kept as it is.
        if reel_path.exists() and not job.args.force:
            old = contracts.read(reel_path, ReelPlan)
            if old.removed:                         # taken out in the review: its plan stays, no reel is made
                entry.status, entry.reason = "skipped", "removed by hand"
                print(f"plan: {entry.tag}: removed by hand")
                continue
            if old.edited:
                reel = old
                entry.length_s, entry.shots = reel.length_s, len(reel.shots)
        if ai_ctx is not None and not reel.edited:      # a model chooses among the possible framings
            from . import aiframe
            from .reel import base_theme
            theme = themes.get(base_theme(reel.theme)[0]) or (job.themes[0] if job.profile.kind != "flight" else None)
            reel = aiframe.apply(reel, scenes.scenes, job.profile, job.mount, theme, ai_ctx, elsewhere=list(shown))
            shown += [f"{reel.title or reel.theme}: " + ", ".join(
                " -> ".join(n if n not in (s.attrs.get("views") or {}) else
                            f"own view yaw {s.attrs['views'][n]['yaw']:.0f} pitch {s.attrs['views'][n]['pitch']:.0f}" for n in s.framing)
                + (" + split frame" if s.second else "") for s in reel.shots)]
        framing = plan_framing(reel, job.profile, job.mount, tele, calibration)
        # An unchanged reel plan keeps its framing plan, so manual edits survive.
        if framing_path.exists() and not job.args.force:
            old_framing = contracts.read(framing_path, FramingPlan)
            if old_framing.source == framing.source:
                framing = old_framing
        contracts.write(reel_path, reel)
        contracts.write(framing_path, framing)
        framings.append(framing)
        print(f"plan: {entry.tag}: {len(reel.shots)} shots, {reel.length_s:.1f}s" + (" (edited)" if reel.edited else ""))
        for s in reel.shots:
            print(f"  {s.index:>2}. {_hms(s.start_s)}  {s.duration_s / s.speed:>5.1f}s  {s.type:<10} {' -> '.join(s.framing)}")
    for entry in entries:
        if entry.status != "planned" and entry.reason != "removed by hand":
            print(f"plan: {entry.tag}: no reel, {entry.reason}")
    contracts.write(job.path("reels.json"), ReelSet(reels=entries))
    if not any(e.status == "planned" for e in entries):
        sys.exit("No reel could be planned: no usable scenes for the chosen themes inside the recording.")
    job.note("plan", reels=[e.tag for e in entries if e.status == "planned"])
    return framings


def _planned(job: Job) -> list[tuple[str, FramingPlan]]:
    reels_path = job.path("reels.json")
    if not reels_path.exists():
        sys.exit("No reels.json yet. Run `plan` first.")
    out = []
    for entry in contracts.read(reels_path, ReelSet).reels:
        if entry.status == "planned":
            out.append((entry.tag, contracts.read(job.path(f"framing_plan_{entry.tag}.json"), FramingPlan)))
    return out


def _scenes(job: Job) -> Scenes | None:
    path = job.path("scenes.json")
    return contracts.read(path, Scenes) if path.exists() else None


def step_render(job: Job) -> list[Path]:
    ctx = job.context(_scenes(job))
    outputs = []
    for tag, framing in _planned(job):
        out = job.path(f"reel_{tag}.mp4")
        print(f"render: {tag}: {len(framing.shots)} shots")
        render.render_reel(framing, ctx, out)
        print(f"render: done -> {out}")
        outputs.append(out)
    job.note("render", outputs=[o.name for o in outputs])
    return outputs


def step_shots(job: Job) -> list[Path]:
    """The shots of every reel as separate files, for editing in a phone app."""
    ctx = job.context(_scenes(job))
    folders = []
    for tag, framing in _planned(job):
        folder = job.path(f"shots_{tag}")
        print(f"shots: {tag}")
        files = render.export_shots(framing, ctx, folder)
        print(f"shots: {len(files)} files -> {folder}")
        folders.append(folder)
    job.note("shots", folders=[f.name for f in folders])
    return folders


def cmd_run(job: Job) -> None:
    step_analyze(job)
    step_plan(job)
    if job.args.review:
        print("review: stopped after planning. Edit the reel plans, then run `render`.")
        return
    step_render(job)
    if job.args.shots:
        step_shots(job)


def cmd_preview(job: Job) -> None:
    out = job.profile.output
    rec = job.recording if job.profile.kind == "flight" else job.recordings[0]
    level = None
    if job.profile.kind != "flight":
        from . import holiday
        level = holiday.clip_level(job.sensors(rec))
    source = rec if job.args.source == "proxy" else rec.best()
    sheet = render.preview_presets(source, job.mount, job.args.at, (out.width // 2, out.height // 2),
                                   job.work, job.path("preview"), job.log, job.profile.safe_zone, level)
    print(f"preview: {sheet}")


def cmd_proxy(job: Job) -> None:
    """Small working copies for takes the camera stored without an LRV file."""
    for rec in job.recordings:
        if rec.kind == "insv":
            proxy = stitch.make_proxy(rec, job.work / "proxy", job.log)
            print(f"proxy: {rec.id} -> {proxy.files[0].parent} ({len(proxy.files)} files)")
        else:
            print(f"proxy: {rec.id} already has one ({rec.kind})")


def cmd_phases(args) -> None:
    profile = load_profile(args.profile)
    fixes = parse_igc(Path(args.igc_file))
    print(f"{args.igc_file}: {len(fixes)} fixes, {_utc(fixes[0].t)} - {_utc(fixes[-1].t)} UTC")
    for ph in detect_phases(fixes, profile.detection, profile.aircraft):
        attrs = ", ".join(f"{k}={v}" for k, v in ph.attrs.items())
        print(f"  {_utc(ph.start)} - {_utc(ph.end)}  {ph.type:<8} {attrs}")


def cmd_profiles(args) -> None:
    for kind, title in (("", "profiles"), ("mounts", "mounts"), ("themes", "themes")):
        print(f"{title}: {', '.join(list_names(kind))}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="insta-roller", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def job_command(name: str, func, help: str):
        p = sub.add_parser(name, help=help)
        p.add_argument("folder", help="job folder with the recordings and, for a flight, the IGC file (read only)")
        p.add_argument("--out", help="output folder (default: <folder>/insta-roller)")
        p.add_argument("--work", help="local cache folder (default: <out>/.cache)")
        p.add_argument("--profile", default="auto", help="auto = flight when the folder holds an IGC file, else holiday")
        p.add_argument("--mount", help="camera mount (default: from the profile)")
        p.add_argument("--themes", help="comma-separated reel themes (default: from the profile)")
        p.add_argument("--ai-framing", choices=["off", "local", "openrouter"],
                       help="a model picks the framing of every shot: local = CLIP on this PC, "
                            "openrouter = a vision model via the OpenRouter API (stills leave the PC; "
                            "needs OPENROUTER_API_KEY). Default: from the profile")
        p.add_argument("--ai-scout", action=argparse.BooleanOptionalAction, default=None,
                       help="openrouter: also look through the whole recording for sights worth a shot (more requests)")
        p.add_argument("--ai-model", help="openrouter: the model, e.g. anthropic/claude-sonnet-5.5 (default: from the profile)")
        p.add_argument("--ai-reasoning", type=int, metavar="TOKENS",
                       help="openrouter: tokens the model may spend reasoning before it answers; 0 = none")
        p.add_argument("--enable", help="comma-separated themes to switch on, e.g. landing")
        p.add_argument("--disable", help="comma-separated themes to switch off")
        p.add_argument("--length", type=float, help="length of every reel in seconds (default: from theme / profile)")
        p.add_argument("--counts", help="how many reels per theme, each from other scenes, e.g. thermal=3,glide_views=2")
        p.add_argument("--lengths", help="length per reel theme in seconds, e.g. thermal=40,surprise=60")
        p.add_argument("--mode", choices=["day", "trip", "both"], default="day",
                       help="holiday: one film per day, one film of the whole trip, or both")
        p.add_argument("--recording", help="recording id when the folder holds several")
        p.add_argument("--igc", help="IGC file when it is not in the folder")
        p.add_argument("--no-igc", action="store_true", help="ignore IGC files; use the camera's GPS or gyroscope")
        p.add_argument("--offset", type=float, default=0.0,
                       help="seconds added to the synchronised video start time (normally not needed)")
        p.add_argument("--video-start", help="UTC time of the first video frame, ISO format; turns automatic sync off")
        p.add_argument("--source", choices=["auto", "proxy", "sdk", "ffmpeg"], default="auto",
                       help="auto = Media SDK stitcher when built, else the camera files stitched roughly by FFmpeg; "
                            "ffmpeg = never the SDK; proxy = LRV only")
        p.add_argument("--overlay", action=argparse.BooleanOptionalAction, default=None,
                       help="flight data overlay on or off (default: from the profile)")
        p.add_argument("--shots", action="store_true", help="run: also export the separate shots")
        p.add_argument("--review", action="store_true", help="run: stop after planning, before rendering")
        p.add_argument("--people", help="comma-separated names to recognise (default: from the profile)")
        p.add_argument("--cache-gb", type=float, default=50.0, help="size limit of the stitched-segment cache")
        p.add_argument("--copy-local", action="store_true", help="copy source files from the NAS to the work folder first")
        p.add_argument("--force", action="store_true", help="ignore cached results")
        p.set_defaults(func=lambda a: func(Job(a)))
        return p

    job_command("run", cmd_run, "analyze, plan and render in one go")
    job_command("analyze", step_analyze, "detect scenes -> scenes.json, telemetry.json")
    job_command("plan", step_plan, "choose reels, shots and framing -> reels.json, reel_plan / framing_plan")
    job_command("render", step_render, "render every planned reel -> reel_<theme>_<length>s.mp4")
    job_command("shots", step_shots, "export the shots of every reel as separate files")
    job_command("preview", cmd_preview, "stills of every preset, to check the mount calibration") \
        .add_argument("--at", type=float, default=60.0, help="video time in seconds")

    job_command("proxy", cmd_proxy, "make low-resolution working copies of takes without an LRV file")

    p = sub.add_parser("phases", help="print the flight phases found in an IGC file")
    p.add_argument("igc_file")
    p.add_argument("--profile", default="flight")
    p.set_defaults(func=cmd_phases)

    p = sub.add_parser("profiles", help="list the profiles, mounts and themes")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("schemas", help="export the JSON schemas of the data contracts")
    p.add_argument("folder")
    p.set_defaults(func=lambda a: contracts.export_schemas(Path(a.folder)))

    from . import faces
    faces.add_commands(sub)

    for stream in (sys.stdout, sys.stderr):         # names with non-Latin letters on a legacy console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

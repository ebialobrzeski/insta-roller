"""Command line: every pipeline step is its own idempotent command."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import contracts, render
from .config import load_mount, load_profile, load_theme
from .contracts import FramingPlan, JobState, RecordingInfo, ReelPlan, Scenes, Sync
from .framing import plan_framing
from .igc import parse_igc
from .ingest import Recording, discover, fingerprint
from .phases import detect_phases
from .reel import plan_reel
from .scenes import build_scenes


def _hms(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")


class Job:
    def __init__(self, args):
        self.args = args
        self.folder = Path(args.folder).resolve()
        self.out = Path(args.out).resolve() if args.out else self.folder / "insta-roller"
        self.work = Path(args.work).resolve() if args.work else self.out / ".cache"
        self.log = self.out / "logs" / "ffmpeg.log"
        self.profile = load_profile(args.profile)
        self.mount = load_mount(args.mount or self.profile.mount)
        self.theme = load_theme(args.theme or self.profile.theme)
        self.length = args.length or self.profile.target_length_s
        self._recording: Recording | None = None
        self._igc: Path | None = None

    # --- inputs -----------------------------------------------------------
    def _discover(self) -> None:
        recordings, igcs = discover(self.folder)
        if not recordings:
            sys.exit(f"No equirectangular .mp4 or LRV proxy files in {self.folder}")
        if self.args.igc:
            igcs = [Path(self.args.igc).resolve()]
        if self.args.recording:
            match = [r for r in recordings if r.id == self.args.recording]
            if not match:
                sys.exit(f"Recording '{self.args.recording}' not found. Available: "
                         + ", ".join(r.id for r in recordings))
            recordings = match
        self._recording = max(recordings, key=lambda r: r.duration_s)
        self._igc = igcs[0] if igcs else None

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
            self._discover()
        return self._recording

    @property
    def igc(self) -> Path:
        if self._recording is None:
            self._discover()
        if self._igc is None:
            sys.exit(f"No IGC file in {self.folder}. Add the flight log or pass --igc. "
                     "Analysis without IGC arrives in stage 3.")
        return self._igc

    # --- outputs ----------------------------------------------------------
    @property
    def tag(self) -> str:
        return f"{self.theme.name}_{self.length:g}s"

    def path(self, name: str) -> Path:
        return self.out / name

    def note(self, step: str, **info) -> None:
        state_path = self.path("job_state.json")
        state = contracts.read(state_path, JobState) if state_path.exists() else JobState()
        state.steps[step] = {"at": datetime.now().isoformat(timespec="seconds"), **info}
        for value in info.values():
            if isinstance(value, str) and value.endswith(".mp4") and value not in state.outputs:
                state.outputs.append(value)
        contracts.write(state_path, state)


def step_analyze(job: Job) -> Scenes:
    rec, igc, profile = job.recording, job.igc, job.profile
    fp = fingerprint(igc, *rec.files, profile.detection.model_dump_json(),
                     {k: v.base_score for k, v in profile.scene_types.items()},
                     profile.thermal_good_climb_ms, job.args.offset, job.args.video_start)
    path = job.path("scenes.json")
    if path.exists() and not job.args.force:
        cached = contracts.read(path, Scenes)
        if cached.fingerprint == fp:
            print(f"analyze: cached ({len(cached.scenes)} scenes)")
            return cached

    if job.args.video_start:
        base = datetime.fromisoformat(job.args.video_start.replace("Z", "+00:00"))
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        start, method = base.timestamp(), "manual start time"
    elif rec.start_utc is not None:
        start, method = rec.start_utc, "camera clock"
    else:
        sys.exit("The recording has no creation time. Pass --video-start (UTC of the first frame).")
    start += job.args.offset
    if job.args.offset:
        method += " + manual offset"

    fixes = parse_igc(igc)
    phases = detect_phases(fixes, profile.detection)
    scenes, missing = build_scenes(phases, start, rec.duration_s, profile)
    result = Scenes(
        fingerprint=fp,
        recording=RecordingInfo(id=rec.id, kind=rec.kind, files=[f.name for f in rec.files],
                                duration_s=round(rec.duration_s, 2)),
        sync=Sync(video_start_utc=start, offset_s=job.args.offset, method=method),
        missing_phases=missing, scenes=scenes)
    contracts.write(path, result)

    lines = [f"Recording {rec.id} ({rec.kind}), {_hms(rec.duration_s)}, starts {_utc(start)} UTC ({method})",
             f"IGC {igc.name}: {_utc(fixes[0].t)} - {_utc(fixes[-1].t)} UTC, {len(phases)} phases", ""]
    for s in scenes:
        attrs = ", ".join(f"{k}={v}" for k, v in s.attrs.items())
        lines.append(f"  {_hms(s.start_s)} - {_hms(s.end_s)}  {s.type:<8} score {s.score:.2f}  {attrs}")
    if missing:
        lines.append(f"\nNot covered by the recording: {', '.join(missing)}")
    report = "\n".join(lines)
    job.path("scenes_report.txt").write_text(report + "\n", encoding="utf-8")
    print(f"analyze: {len(scenes)} scenes -> {path}")
    print(report)
    job.note("analyze", fingerprint=fp, scenes=len(scenes))
    return result


def step_plan(job: Job) -> FramingPlan:
    scenes_path = job.path("scenes.json")
    if not scenes_path.exists():
        sys.exit("No scenes.json yet. Run `analyze` first.")
    scenes = contracts.read(scenes_path, Scenes)
    reel = plan_reel(scenes.scenes, job.profile, job.theme, job.mount, job.length)
    if not reel.shots:
        sys.exit("No usable scenes for this theme inside the recording.")
    reel_path = job.path(f"reel_plan_{job.tag}.json")
    framing_path = job.path(f"framing_plan_{job.tag}.json")

    # An unchanged reel plan keeps the framing plan, so manual edits survive.
    unchanged = reel_path.exists() and contracts.read(reel_path, ReelPlan) == reel
    if unchanged and framing_path.exists() and not job.args.force:
        print(f"plan: cached -> {framing_path.name}")
        return contracts.read(framing_path, FramingPlan)

    framing = plan_framing(reel, job.profile, job.mount)
    contracts.write(reel_path, reel)
    contracts.write(framing_path, framing)
    print(f"plan: {len(reel.shots)} shots, {reel.length_s:.1f}s (target {job.length:g}s) -> {framing_path.name}")
    for s in reel.shots:
        print(f"  {s.index:>2}. {_hms(s.start_s)}  {s.duration_s:>5.1f}s  {s.type:<8} {' -> '.join(s.framing)}")
    job.note("plan", reel_plan=reel_path.name, framing_plan=framing_path.name)
    return framing


def step_render(job: Job) -> Path:
    framing_path = job.path(f"framing_plan_{job.tag}.json")
    if not framing_path.exists():
        sys.exit(f"No {framing_path.name} yet. Run `plan` first.")
    framing = contracts.read(framing_path, FramingPlan)
    out = job.path(f"reel_{job.tag}.mp4")
    print(f"render: {len(framing.shots)} shots from {job.recording.id}")
    render.render_reel(framing, job.recording, job.mount, job.profile.encoder, job.work, out, job.log)
    print(f"render: done -> {out}")
    job.note("render", output=out.name)
    return out


def cmd_run(job: Job) -> None:
    step_analyze(job)
    step_plan(job)
    step_render(job)


def cmd_preview(job: Job) -> None:
    out = job.profile.output
    sheet = render.preview_presets(job.recording, job.mount, job.args.at, out.width // 2, out.height // 2,
                                   job.work, job.path("preview"), job.log)
    print(f"preview: {sheet}")


def cmd_phases(args) -> None:
    profile = load_profile(args.profile)
    fixes = parse_igc(Path(args.igc_file))
    print(f"{args.igc_file}: {len(fixes)} fixes, {_utc(fixes[0].t)} - {_utc(fixes[-1].t)} UTC")
    for ph in detect_phases(fixes, profile.detection):
        attrs = ", ".join(f"{k}={v}" for k, v in ph.attrs.items())
        print(f"  {_utc(ph.start)} - {_utc(ph.end)}  {ph.type:<8} {attrs}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="insta-roller", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def job_command(name: str, func, help: str):
        p = sub.add_parser(name, help=help)
        p.add_argument("folder", help="job folder with the recording and the IGC file (read only)")
        p.add_argument("--out", help="output folder (default: <folder>/insta-roller)")
        p.add_argument("--work", help="local cache folder (default: <out>/.cache)")
        p.add_argument("--profile", default="flight")
        p.add_argument("--mount", help="camera mount (default: from the profile)")
        p.add_argument("--theme", help="reel theme (default: from the profile)")
        p.add_argument("--length", type=float, help="reel length in seconds (default: from the profile)")
        p.add_argument("--recording", help="recording id when the folder holds several")
        p.add_argument("--igc", help="IGC file when it is not in the folder")
        p.add_argument("--offset", type=float, default=0.0,
                       help="seconds added to the video start time to match the IGC clock")
        p.add_argument("--video-start", help="UTC time of the first video frame, ISO format")
        p.add_argument("--force", action="store_true", help="ignore cached results")
        p.set_defaults(func=lambda a: func(Job(a)))
        return p

    job_command("run", cmd_run, "analyze, plan and render in one go")
    job_command("analyze", step_analyze, "detect scenes -> scenes.json")
    job_command("plan", step_plan, "choose shots and framing -> reel_plan / framing_plan")
    job_command("render", step_render, "render the framing plan -> reel .mp4")
    job_command("preview", cmd_preview, "stills of every preset, to check the mount calibration") \
        .add_argument("--at", type=float, default=60.0, help="video time in seconds")

    p = sub.add_parser("phases", help="print the flight phases found in an IGC file")
    p.add_argument("igc_file")
    p.add_argument("--profile", default="flight")
    p.set_defaults(func=cmd_phases)

    p = sub.add_parser("schemas", help="export the JSON schemas of the data contracts")
    p.add_argument("folder")
    p.set_defaults(func=lambda a: contracts.export_schemas(Path(a.folder)))

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

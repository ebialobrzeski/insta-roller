"""The local web server: the API for the browser UI, and the UI itself.

It never runs FFmpeg or the pipeline (app.runner does, as CLI subprocesses). It keeps the
queue, edits YAML and JSON files, and serves what the steps wrote into a job's output folder.
It writes only there, into profiles/ and into the app's data folder. There is no login.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.reel import base_theme
from core.config import PROFILES_DIR, Mount, Profile, Theme, list_names, load_mount, load_profile, load_theme

from . import people as people_page
from .db import Busy, Queue
from .runner import run_subprocess
from .steps import JOB_PROFILE, OUT_DIR, cli_args, job_work, pipeline, python_exe, render_steps
from .steps import out_dir as job_out

STATIC = Path(__file__).parent / "static"
REPO = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[A-Za-z0-9_\-]+")
KINDS = {"profiles": ("", Profile), "mounts": ("mounts", Mount), "themes": ("themes", Theme)}
VIDEO = {".insv", ".lrv", ".mp4"}
QUALITY = {"best": "sdk", "good": "ffmpeg", "fast": "proxy"}     # the form's three choices -> --source
PHASE = {"analyze": 0, "plan": 1, "thumbnails": 1, "render": 2, "shots": 2}    # scenes, plan, render
SHOT = re.compile(r"shot (\d+)/(\d+)")
TAG = re.compile(r"(.+?)_(\d+)s")       # a reel's tag: theme and length, as in reel_<tag>.mp4, shots_<tag>/
POINTS = 300                            # barogram points sent to the browser


class JobOptions(BaseModel):
    """What the "Nowe zadanie" form sends; every field maps to a CLI option (app.steps.cli_args)."""
    model_config = ConfigDict(extra="forbid")
    out: str = ""                           # "folder wyników"; empty = <job folder>/insta-roller
    profile: str = "auto"
    profile_yaml: str = ""                  # profile "inne": the job's own settings
    mount: str = ""
    length: float | None = Field(None, gt=0)
    lengths: dict[str, float] = {}          # theme -> seconds, for the reels whose length was changed
    counts: dict[str, int] = {}             # theme -> how many reels of it (each from other scenes)
    enable: list[str] = []                  # themes switched on for this job, e.g. landing
    disable: list[str] = []
    mode: Literal["day", "trip", "both"] = "day"
    people: list[str] = []
    overlay: bool | None = None
    ai_framing: Literal["off", "local", "openrouter"] | None = None   # None = as the profile says
    ai_model: str = Field("", max_length=120, pattern=r"^[\w.:/@-]*$")  # OpenRouter model; empty = as the profile says
    ai_reasoning: int | None = Field(None, ge=0, le=200_000)          # reasoning tokens; None = as the profile says
    ai_scout: bool | None = None            # the AI also looks through the whole recording for sights
    review: bool = False                    # stop after analysis + plan for the scene review
    shots: bool = False                     # also export the separate shots
    films: bool = True                      # False = "same ujęcia": the separate shots, no finished film
    source: Literal["auto", "proxy", "sdk", "ffmpeg"] = "auto"
    quality: Literal["best", "good", "fast"] | None = None      # the form's name for a source, see QUALITY


class NewJob(BaseModel):
    folder: str
    options: JobOptions = JobOptions()


class Settings(BaseModel):
    """The app's own settings, kept in <data>/settings.json."""
    model_config = ConfigDict(extra="forbid")
    work: str = ""                          # local cache folder (--work); empty = <output folder>/.cache
    quality: Literal["auto", "best", "good", "fast"] = "auto"   # what the "Nowy film" form starts with
    people_names: list[str] = []            # names exported from Immich; empty = the holiday profile's people
    # Who frames the shots, unless a job says otherwise: "" = as the profile says (fixed framings).
    ai_framing: Literal["", "off", "local", "openrouter"] = ""
    ai_model: str = Field("", max_length=120, pattern=r"^[\w.:/@-]*$")
    ai_reasoning: int = Field(0, ge=0, le=200_000)
    ai_scout: bool = False                  # with the remote AI: look through the whole recording for sights
    # What the settings form changed, per profile: laid over the profile's file when a job is made,
    # so the files (and their comments) stay as they are. {"flight": {"output": {"fps": 60}}}
    overrides: dict[str, dict] = {}
    lengths: dict[str, float] = {}          # reel theme -> seconds, for every new job


class Text(BaseModel):
    text: str


class Compose(BaseModel):
    base: str
    changes: dict


class Version(BaseModel):
    length: float = Field(gt=0)


class RenderAsk(BaseModel):
    quality: Literal["best", "good", "fast"] | None = None      # None = as the job was started


class ResetAsk(BaseModel):
    tags: list[str] = Field(max_length=64)                      # reels to plan again, automatically
    again: bool = False                                         # True = "make it anew": other moments and framings


class PreviewAsk(BaseModel):
    """Pictures the review screen wants: stills of a moment through a view, and one shot as a clip."""
    stills: list[dict] = Field([], max_length=120)      # {"shot": {scene_id, recording, attrs}, "at": s, "preset": name}
    clip: dict | None = None                            # a shot of a reel plan


class ReviewEdit(BaseModel):
    reels: dict[str, list[dict]]            # reel tag -> its shots in film order


def inside(base: Path, rel: str) -> Path:
    """`rel` below `base`, or 400: nothing outside a job's output folder is served from it."""
    rel = rel.replace("\\", "/").strip("/")
    base = base.resolve()
    path = (base / rel).resolve()
    if ":" in rel or rel.startswith("~") or not path.is_relative_to(base):
        raise HTTPException(400, "Ścieżka poza folderem wyników")
    return path


def absolute(text: str) -> Path:
    """A full path typed or picked in the UI (drive letter or UNC share), normalised; else 400.
    Not resolved: a mapped drive keeps its letter."""
    text = text.strip().strip('"')
    drive, tail = os.path.splitdrive(text)
    if "\0" in text or not tail.startswith(("/", "\\")) or (os.name == "nt" and not drive):
        raise HTTPException(400, "Podaj pełną ścieżkę folderu, np. D:\\Wideo\\lot albo \\\\nas\\wideo\\lot")
    return Path(os.path.abspath(text))


def drives() -> list[str]:
    """Where the folder picker starts: every drive letter, mapped network drives included."""
    if os.name != "nt":
        return ["/"]
    return [d for d in os.listdrives() if os.path.isdir(d)]


def same(a: Path, b: Path) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def zip_stream(files: list[Path]):
    """A zip of the files, built while it is sent (stored, not compressed: it is video)."""
    class Sink:
        def __init__(self):
            self.data = bytearray()

        def write(self, chunk):
            self.data += chunk
            return len(chunk)

        def flush(self):
            pass

    sink = Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as archive:
        for file in files:
            with open(file, "rb") as src, archive.open(zipfile.ZipInfo.from_file(file, file.name), "w") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    yield bytes(sink.data)
                    sink.data.clear()
    yield bytes(sink.data)


def merge(data: dict, changes: dict) -> dict:
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merge(data[key], value)
        else:
            data[key] = value
    return data


def names_of(data) -> list[str]:
    """People names from people.json, whichever shape it has: list, {name: ...} or {"people": ...}."""
    if isinstance(data, dict):
        return names_of(data["people"]) if "people" in data else [str(k) for k in data]
    if isinstance(data, list):
        return [str(p.get("name", "")) if isinstance(p, dict) else str(p) for p in data]
    return []


def find_stitcher() -> Path | None:
    """The Media SDK stitcher, when it is built: the "Najlepsza" quality needs it."""
    try:
        from core import stitch
        return stitch.find_exe()
    except Exception:
        return None


@functools.lru_cache(1)
def gpu_name() -> str:
    """The graphics card as nvidia-smi names it; empty when it cannot be asked."""
    try:
        hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        asked = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=3, **hidden)
        return asked.stdout.strip().splitlines()[0].strip() if asked.returncode == 0 and asked.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def probe_recordings(folder: Path) -> tuple[int, float]:
    """Takes in a folder and their total length, as the CLI will see them (reads the file headers)."""
    from core.ingest import discover
    recordings, _ = discover(folder)
    return len(recordings), sum(r.duration_s for r in recordings)


def thin(samples: list[dict], limit: int = POINTS) -> list[dict]:
    """About `limit` samples, evenly spaced; in every stretch the one that keeps the peaks."""
    if len(samples) <= limit:
        return samples
    size = math.ceil(len(samples) / (limit // 2))
    kept = []
    for i in range(0, len(samples), size):
        part = samples[i:i + size]
        low, high = min(part, key=lambda s: s["alt_m"]), max(part, key=lambda s: s["alt_m"])
        kept += sorted({id(low): low, id(high): high}.values(), key=lambda s: s["t"])
    return kept


def activity_text(jobs: list[dict]) -> str:
    """What the app is doing now, in words: the tray icon's line (the page has its own, in ui.js)."""
    job = next((j for j in jobs if j["status"] == "running"), None)
    if job is None:
        return "Zaraz zaczynam…" if any(j["status"] == "queued" for j in jobs) else "Bezczynna"
    stage = job["steps"][job["step_index"]] if job["step_index"] < len(job["steps"]) else ""
    shot = SHOT.findall(job["log"].rpartition("$ ")[2])[-1:]
    of = f" {shot[0][0]} z {shot[0][1]}" if shot else ""
    words = {"analyze": "Szukam scen…", "plan": "Układam plan…", "thumbnails": f"Robię miniatury{of or '…'}",
             "render": f"Renderuję ujęcie{of}" if of else "Renderuję…", "shots": f"Zapisuję ujęcie{of}" if of else "Zapisuję ujęcia…"}
    return f"{Path(job['folder']).name}: {words.get(stage, 'Pracuję…')}"


def day_text(day: str) -> str:
    """20250812 -> 12.08.2025"""
    return f"{day[6:8]}.{day[4:6]}.{day[:4]}" if len(day) == 8 and day.isdigit() else day


MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]


def day_words(day: str) -> str:
    """20260623 -> 23 czerwca 2026"""
    if len(day) != 8 or not day.isdigit() or not 1 <= int(day[4:6]) <= 12:
        return day
    return f"{int(day[6:8])} {MONTHS[int(day[4:6]) - 1]} {day[:4]}"


def recording_label(kind: str, folder: str, scenes: dict | None) -> str:
    """What a folder of recordings is called in the app: "Lot 23 czerwca 2026", "Wyjazd 2–19 sierpnia 2025".
    The dates come from the recordings; a date in the folder name is the fallback."""
    days = sorted({r.get("day", "") for r in (scenes or {}).get("recordings", []) if r.get("day")})
    start = ((scenes or {}).get("recording") or {}).get("start_utc")
    if not days and start:
        days = [time.strftime("%Y%m%d", time.localtime(start))]
    if not days:
        found = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", Path(folder).name)
        days = ["".join(found.groups())] if found else []
    word = "Wyjazd" if kind == "holiday" else "Lot"
    if not days or not days[0].isdigit():
        return f"{word}: {Path(folder).name or folder}"
    first, last = days[0], days[-1]
    if first == last:
        return f"{word} {day_words(first)}"
    if first[:6] == last[:6]:
        return f"{word} {int(first[6:8])}–{day_words(last)}"
    return f"{word} {day_words(first)} – {day_words(last)}"


class FreshFiles(StaticFiles):
    """The UI's files, checked for changes on every load: an updated app never runs with old JS."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def default_data_dir() -> Path:
    """%LOCALAPPDATA%\\insta-roller: the queue and the app's settings."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "insta-roller"


def create_app(data: str | Path | None = None, profiles: str | Path | None = None,
               people: str | Path | None = None, env_file: str | Path | None = None,
               stitcher=find_stitcher, runner=run_subprocess) -> FastAPI:
    """`data`: the app's folder (queue.db, settings.json). `profiles`: the profiles the CLI uses,
    edited in place. `people`: the people.json whose names the form offers and "Osoby" refreshes.
    `env_file`: where the Immich address and key are, besides the environment. `stitcher`: returns
    the Media SDK stitcher or None. `runner`: runs the Immich export, as app.runner.run_subprocess."""
    data = Path(data or default_data_dir())
    profiles = Path(profiles or PROFILES_DIR)
    people_file = Path(people or REPO / "people.json")
    kinds: dict[tuple, str] = {}                        # flight / holiday per job, see kind_of
    queue = Queue(data / "queue.db")
    app = FastAPI(title="insta-roller", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.queue, app.state.data, app.state.profiles = queue, data, profiles

    # --- helpers ----------------------------------------------------------
    def settings() -> dict:
        try:
            return Settings.model_validate(read_json(data / "settings.json") or {}).model_dump()
        except ValidationError:
            return Settings().model_dump()

    def save(values: dict) -> None:
        data.mkdir(parents=True, exist_ok=True)
        (data / "settings.json").write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")

    app.state.work = lambda: settings()["work"]         # for the job runner

    def need(job_id: int) -> dict:
        job = queue.get(job_id)
        if job is None:
            raise HTTPException(404, "Nie ma takiego zadania")
        return job

    def out_dir(job: dict) -> Path:
        return Path(job_out(job["folder"], job["options"]))

    def kind_of(job: dict) -> str:
        """flight or holiday, as the CLI will decide; remembered, so polling does not read the folder."""
        key = (job["id"], job["options"].get("profile"), job["options"].get("profile_yaml"))
        if key not in kinds:
            profile, _ = job_config(job)
            if profile is None:
                return "flight"
            kinds[key] = "holiday" if profile.kind == "holiday" else "flight"
        return kinds[key]

    labels: dict = {}

    def label_of(job: dict) -> str:
        """Read once per state of the analysis: polling must not read scenes.json every second."""
        scenes_path = out_dir(job) / "scenes.json"
        try:
            stamp = scenes_path.stat().st_mtime
        except OSError:
            stamp = 0
        key = (job["id"], stamp)
        if key not in labels:
            labels[key] = recording_label(kind_of(job), job["folder"], read_json(scenes_path) if stamp else None)
        return labels[key]

    def view(job: dict, log_lines: int | None = None) -> dict:
        job = dict(job)
        step = job["log"].rpartition("$ ")[2]           # the output of the step that runs now
        shot = SHOT.findall(step)[-1:] if job["status"] == "running" else []
        job["detail"] = {"shot": int(shot[0][0]), "of": int(shot[0][1])} if shot else None
        if log_lines:
            job["log"] = "\n".join(job["log"].splitlines()[-log_lines:])
        work = job_work(settings()["work"], job["id"])
        job["name"], job["kind"] = Path(job["folder"]).name or job["folder"], kind_of(job)
        job["label"] = label_of(job)
        job["phase"] = PHASE.get(job["stage"], 3 if job["status"] in ("done", "review") else 0)
        job["quality"] = {v: k for k, v in QUALITY.items()}.get(job["options"].get("source"), "auto")
        job["elapsed_s"] = round((time.time() if job["status"] == "running" else job["updated"]) - job["queued"])
        job["out"] = str(out_dir(job))
        job["commands"] = ["python -m " + subprocess.list2cmdline(cli_args(step, job["folder"], job["options"], work))
                           for step in job["steps"]]
        return job

    def parse(kind: str, text: str, name: str | None = None):
        """YAML text -> validated Profile / Mount / Theme, or 422 with the reason."""
        try:
            item = KINDS[kind][1].model_validate(yaml.safe_load(text))
        except (yaml.YAMLError, ValidationError) as error:
            raise HTTPException(422, str(error))
        problems = []
        if name is not None and item.name != name:
            problems.append(f"pole name ({item.name}) musi być takie jak nazwa pliku ({name})")
        if kind == "profiles":
            if item.mount not in list_names("mounts", profiles):
                problems.append(f"nie ma montażu '{item.mount}'")
            problems += [f"nie ma tematu '{t}'" for t in item.themes if t not in list_names("themes", profiles)]
        if problems:
            raise HTTPException(422, "; ".join(problems))
        return item

    def config_path(kind: str, name: str) -> Path:
        if kind not in KINDS or not NAME.fullmatch(name):
            raise HTTPException(400, "Zła nazwa: dozwolone litery, cyfry, _ i -")
        return profiles / KINDS[kind][0] / f"{name}.yaml"

    def job_config(job: dict) -> tuple[Profile | None, Mount | None]:
        """Profile and mount of a job as the CLI would pick them; None when they cannot be loaded here."""
        options, profile, mount = job["options"], None, None
        try:
            if options.get("profile_yaml"):
                profile = Profile.model_validate(yaml.safe_load(options["profile_yaml"]))
            else:
                name = options.get("profile") or "auto"
                if name == "auto":
                    folder = Path(job["folder"])
                    name = "flight" if any(p.suffix.lower() == ".igc" for p in folder.iterdir()) else "holiday"
                profile = load_profile(name, profiles)
            mount = load_mount(options.get("mount") or profile.mount, profiles)
        except Exception:               # core is free to change; the review then works without presets
            pass
        return profile, mount

    def default_shot(scene: dict, profile: Profile | None, mount: Mount | None) -> dict:
        """A shot for a scene added by hand: the middle of the scene, the profile's framing."""
        kind = profile.scene_types.get(scene["type"]) if profile else None
        speed = kind.speed if kind else 1.0
        length = round(min(scene["end_s"] - scene["start_s"], (kind.max_shot_s if kind else 6.0) * speed), 2)
        framing = list(mount.presets)[:1] if mount else []
        try:
            from core.reel import resolve_framing
            framing = resolve_framing(kind.framing, SimpleNamespace(attrs=scene.get("attrs", {})), mount)
        except Exception:
            pass
        return {"index": 0, "scene_id": scene["id"], "type": scene["type"], "framing": framing,
                "start_s": round((scene["start_s"] + scene["end_s"] - length) / 2, 2), "duration_s": length,
                "recording": scene.get("recording", ""), "speed": speed,
                "attrs": {k: v for k, v in scene.get("attrs", {}).items() if k.startswith("yaw_")}}

    # --- UI: folders and meta ---------------------------------------------
    @app.get("/api/folders")
    def folders(path: str = ""):
        """The folder picker. No path: the drives. Any full path works, UNC shares included."""
        if not path.strip():
            return {"path": "", "parent": None, "crumbs": [], "dirs": [{"name": d, "path": d} for d in drives()],
                    "videos": 0, "igc": 0, "processed": False, "job": None, "out": "",
                    "recent": [{"name": Path(j["folder"]).name, "path": j["folder"]} for j in queue.list()[:6]]}
        here = absolute(path)
        if not here.is_dir():
            raise HTTPException(404, "Nie ma takiego folderu")
        dirs, videos, igc = [], 0, 0
        try:
            entries = sorted(here.iterdir(), key=lambda p: p.name.lower())
        except OSError as error:
            raise HTTPException(403, f"Nie można otworzyć folderu: {error.strerror or error}")
        for p in entries:
            try:
                if p.is_dir() and p.name != OUT_DIR and not p.name.startswith((".", "@", "#", "$")):
                    dirs.append({"name": p.name, "path": str(p)})
                elif p.is_file():
                    videos += p.suffix.lower() in VIDEO
                    igc += p.suffix.lower() == ".igc"
            except OSError:
                pass
        parts = [here, *here.parents][::-1]             # the drive or share first
        job = queue.find(str(here))
        return {"path": str(here), "parent": str(here.parent) if here.parent != here else "",
                "crumbs": [{"name": p.name or str(p), "path": str(p)} for p in parts],
                "dirs": dirs, "videos": videos, "igc": igc, "processed": (here / OUT_DIR).is_dir(),
                "job": job and job["id"], "out": str(here / OUT_DIR)}

    @app.get("/api/folders/inspect")
    def inspect(path: str):
        """What a folder holds, for the sentence under the picker. Reads every file's header: seconds on a NAS."""
        here = absolute(path)
        if not here.is_dir():
            raise HTTPException(404, "Nie ma takiego folderu")
        try:
            found = Counter(p.suffix.lower() for p in here.iterdir() if p.is_file())
        except OSError as error:
            raise HTTPException(403, f"Nie można otworzyć folderu: {error.strerror or error}")
        try:
            recordings, duration = probe_recordings(here)
        except Exception:                               # FFmpeg missing, a share that went away
            recordings, duration = None, None
        return {"path": str(here), "recordings": recordings, "duration_s": duration and round(duration, 1),
                "full_quality": found[".insv"], "previews": found[".lrv"], "films": found[".mp4"],
                "igc": found[".igc"], "kind": "flight" if found[".igc"] else "holiday",
                "usable": bool(recordings) if recordings is not None else bool(sum(found[e] for e in VIDEO)),
                "out": str(here / OUT_DIR)}

    @app.get("/api/meta")
    def meta():
        """Everything the forms offer: profiles, mounts, themes, known people."""
        def each(kind: str, describe) -> list[dict]:
            items = []
            for name in list_names(KINDS[kind][0], profiles):
                try:
                    item = KINDS[kind][1].model_validate(yaml.safe_load(config_path(kind, name).read_text("utf-8")))
                    items.append({"name": name, **describe(item)})
                except Exception as error:
                    items.append({"name": name, "error": str(error)})
            return items

        found = each("profiles", lambda p: {"kind": p.kind, "mount": p.mount, "themes": p.themes,
                                             "length": p.target_length_s, "overlay": p.overlay.enabled,
                                             "people": list(p.people), "ai": p.ai_framing.engine,
                                             "ai_model": p.ai_framing.model, "ai_reasoning": p.ai_framing.reasoning_tokens})
        people = names_of(read_json(people_file))
        for p in found:
            people += [n for n in p.get("people", []) if n not in people]
        return {"profiles": found, "people": [n for n in people if n], "stitcher": stitcher() is not None,
                "quality": settings()["quality"],
                "ai": {k: settings()[k] for k in ("ai_framing", "ai_model", "ai_reasoning", "ai_scout")},
                "lengths": settings()["lengths"],
                "mounts": each("mounts", lambda m: {"presets": list(m.presets)}),
                "themes": each("themes", lambda t: {"title": t.title, "enabled": t.enabled,
                                                    "length": t.target_length_s})}

    # --- UI: jobs ---------------------------------------------------------
    ai_models: dict = {}

    @app.get("/api/ai/models")
    def ai_model_list():
        """Vision models OpenRouter offers, for the wizard's list. Read once per run of the app."""
        if "items" not in ai_models:
            from core import aiframe
            try:
                ai_models["items"] = aiframe.list_models()
            except aiframe.Unavailable as error:
                return {"models": [], "error": str(error)}
        return {"models": ai_models["items"]}

    @app.get("/api/jobs")
    def jobs():
        return [view(j, 12) for j in queue.list()]

    @app.post("/api/jobs")
    def create(body: NewJob):
        folder = absolute(body.folder)
        if not folder.is_dir():
            raise HTTPException(404, "Nie ma takiego folderu")
        options = body.options.model_dump()
        quality = options.pop("quality")
        if quality:
            options["source"] = QUALITY[quality]
        if options["source"] == "sdk" and stitcher() is None:
            raise HTTPException(422, "Najlepsza jakość wymaga programu do sklejania Media SDK, a nie ma go na tym komputerze")
        options["shots"] = options["shots"] or not options["films"]
        chosen = settings()                         # what the settings say, where the form said nothing
        if options["ai_framing"] is None and chosen["ai_framing"]:
            options["ai_framing"] = chosen["ai_framing"]
        if not options["ai_model"] and chosen["ai_model"]:
            options["ai_model"] = chosen["ai_model"]
        if options["ai_reasoning"] is None and chosen["ai_reasoning"]:
            options["ai_reasoning"] = chosen["ai_reasoning"]
        if options["ai_scout"] is None and chosen["ai_scout"] and options["ai_framing"] == "openrouter":
            options["ai_scout"] = True
        current = queue.find(str(folder))
        if options["out"].strip():
            out = absolute(options["out"])
            if same(out, folder):       # films next to the recordings would be taken for recordings
                raise HTTPException(422, "Folder wyników nie może być folderem z nagraniami")
            for other in queue.list():
                if same(out, out_dir(other)) and not (current and other["id"] == current["id"]):
                    raise HTTPException(422, f"Ten folder wyników należy do zadania nr {other['id']}")
            options["out"] = "" if same(out, folder / OUT_DIR) else str(out)
        else:
            options["out"] = ""
        if not options["profile_yaml"]:             # the settings form's changes ride along as the job's own profile
            name = options["profile"]
            if name == "auto":
                name = "flight" if any(p.suffix.lower() == ".igc" for p in folder.iterdir() if p.is_file()) else "holiday"
            if chosen["overrides"].get(name) and config_path("profiles", name).exists():
                options["profile_yaml"] = yaml.safe_dump(effective(name), sort_keys=False, allow_unicode=True)
        for theme, seconds in chosen["lengths"].items():
            options["lengths"].setdefault(theme, seconds)
        if options["profile_yaml"]:
            parse("profiles", options["profile_yaml"])
        elif options["profile"] != "auto" and options["profile"] not in list_names("", profiles):
            raise HTTPException(422, f"Nie ma profilu '{options['profile']}'")
        if options["mount"] and options["mount"] not in list_names("mounts", profiles):
            raise HTTPException(422, f"Nie ma montażu '{options['mount']}'")
        if not all(NAME.fullmatch(t) for t in options["enable"] + options["disable"]):
            raise HTTPException(422, "Zła nazwa tematu")
        options["people"] = [p.strip() for p in options["people"] if p.strip()]
        if any("," in p or p.startswith("-") for p in options["people"]):
            raise HTTPException(422, "Zła nazwa osoby")
        if not (current and current["status"] == "running"):
            out = Path(job_out(str(folder), options))
            try:
                if options["out"] or options["profile_yaml"]:
                    out.mkdir(parents=True, exist_ok=True)
                if options["profile_yaml"]:
                    (out / JOB_PROFILE).write_text(options["profile_yaml"], encoding="utf-8")
            except OSError as error:
                raise HTTPException(422, f"Nie można zapisać w folderze wyników: {error.strerror or error}")
        job, existing = queue.submit(str(folder), options, *pipeline(options))
        return {**view(job), "existing": existing}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: int):
        return view(need(job_id))

    def change(job_id: int, action) -> dict:
        need(job_id)
        try:
            return view(action())
        except Busy:
            raise HTTPException(409, "Zadanie jest w kolejce albo właśnie się wykonuje")

    @app.delete("/api/jobs/{job_id}")
    def forget(job_id: int):
        """"Usuń z listy": the project leaves the list; the recordings and the films stay on the disk."""
        need(job_id)
        try:
            queue.delete(job_id)
        except Busy:
            raise HTTPException(409, "Najpierw zatrzymaj to zadanie")
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        return change(job_id, lambda: queue.cancel(job_id))

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: int):
        return change(job_id, lambda: queue.retry(job_id))

    def rerender(job_id: int, **changed) -> dict:
        job = need(job_id)
        if not (out_dir(job) / "scenes.json").exists():
            raise HTTPException(409, "Zadanie nie ma jeszcze analizy (scenes.json)")
        options = {**job["options"], **changed}
        return change(job_id, lambda: queue.restart(job_id, render_steps(options), "done", options))

    @app.post("/api/jobs/{job_id}/version")
    def new_version(job_id: int, body: Version):
        """Another length from the same analysis: plan + render only."""
        return rerender(job_id, length=body.length)

    @app.post("/api/jobs/{job_id}/render")
    def render(job_id: int, body: RenderAsk | None = None):
        """"Zrób rolki" after the scene review: plan (keeps the edited reel plans) + render."""
        changed = {}
        if body and body.quality:
            if body.quality == "best" and stitcher() is None:
                raise HTTPException(422, "Najlepsza jakość wymaga programu do sklejania Media SDK, a nie ma go na tym komputerze")
            changed["source"] = QUALITY[body.quality]
        return rerender(job_id, **changed, review=False)

    @app.post("/api/jobs/{job_id}/review/reset")
    def review_reset(job_id: int, body: ResetAsk):
        """"Przywróć automatyczne": the named reels lose their hand edits and are planned again."""
        job = need(job_id)
        out = out_dir(job)
        for tag in body.tags:
            if not NAME.fullmatch(tag):
                raise HTTPException(422, "Zła nazwa rolki")
            path = out / f"reel_plan_{tag}.json"
            plan = read_json(path)
            if plan is None:
                raise HTTPException(404, f"Nie ma rolki '{tag}'")
            plan["edited"], plan["removed"] = False, False
            if body.again:
                plan["take"] = int(plan.get("take") or 0) + 1
            path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), "utf-8")
        return change(job_id, lambda: queue.restart(job_id, ["plan", "thumbnails"], "review", job["options"]))

    # --- UI: results ------------------------------------------------------
    def films_in(out: Path) -> tuple[list[dict], object]:
        """The finished films of an output folder, newest first, each with a title a person can read;
        and the function that reads a reel tag that way."""
        titles = {}
        for name in list_names("themes", profiles):
            try:
                titles[name] = yaml.safe_load(config_path("themes", name).read_text("utf-8")).get("title") or name
            except Exception:
                pass

        def named(tag: str) -> dict:
            match = TAG.fullmatch(tag)
            theme = match[1] if match else tag
            base, number = base_theme(theme)        # thermal_p2: the second reel of "thermal"
            if base in titles:
                done = {"tag": tag, "theme": theme, "title": titles[base] + (f" {number}" if number > 1 else ""),
                        "length_s": int(match[2]) if match else None}
                return done
            title = (titles.get(theme) or (f"Dzień {day_text(theme[4:])}" if theme.startswith("day_") else
                                           "Cały wyjazd" if theme == "trip" else theme))
            return {"tag": tag, "theme": theme, "title": title, "length_s": int(match[2]) if match else None}

        planned = {r.get("tag") for r in (read_json(out / "reels.json") or {}).get("reels", []) if r.get("status") == "planned"}
        films = []
        for p in out.glob("reel_*.mp4"):
            stat, tag = p.stat(), p.stem[5:]
            films.append({"name": p.name, "path": str(p), "size": stat.st_size, "mtime": stat.st_mtime, **named(tag),
                          "current": tag in planned})    # False: an earlier version, of another length
        return sorted(films, key=lambda f: -f["mtime"]), named

    @app.get("/api/films")
    def films():
        """Every finished film of every job, newest first: the overview's grid."""
        found = []
        for job in queue.list():
            try:
                found += [{**film, "job": job["id"], "job_name": Path(job["folder"]).name, "job_label": label_of(job)} for film in films_in(out_dir(job))[0]]
            except OSError:                             # a share that is not connected now
                pass
        return sorted(found, key=lambda f: -f["mtime"])

    @app.get("/api/jobs/{job_id}/timeline")
    def timeline(job_id: int):
        """The picture of a job on the overview: a flight's altitude with the chosen shots on it,
        or a holiday's days with their clips."""
        job = need(job_id)
        out = out_dir(job)
        scenes = read_json(out / "scenes.json")
        if scenes is None:
            raise HTTPException(409, "Zadanie nie ma jeszcze analizy (scenes.json)")
        days = {s["id"]: s.get("attrs", {}).get("day", "") for s in scenes.get("scenes", [])}
        shots = []
        for entry in (read_json(out / "reels.json") or {}).get("reels", []):
            plan = read_json(out / f"reel_plan_{entry.get('tag')}.json") if entry.get("status") == "planned" else None
            shots += [{"reel": entry["tag"], "theme": entry.get("theme", ""), "type": s["type"],
                       "start_s": s["start_s"], "duration_s": s["duration_s"],
                       "day": days.get(s["scene_id"], "")} for s in (plan or {}).get("shots", [])]
        recordings = scenes.get("recordings") or [r for r in [scenes.get("recording")] if r]
        if scenes.get("source") == "holiday" or any(r.get("day") for r in recordings):
            strip: dict[str, dict] = {}
            for r in recordings:
                day = strip.setdefault(r.get("day", ""), {"day": r.get("day", ""), "clips": 0, "duration_s": 0.0, "shots": 0})
                day["clips"] += 1
                day["duration_s"] = round(day["duration_s"] + r.get("duration_s", 0.0), 1)
            for day in strip.values():
                day["label"] = day_text(day["day"])
                day["shots"] = len({(s["start_s"], s["day"]) for s in shots if s["day"] == day["day"]})
            return {"kind": "holiday", "days": [strip[d] for d in sorted(strip)], "shots": len(shots)}
        samples = (read_json(out / "telemetry.json") or {}).get("samples", [])
        return {"kind": "flight", "job": job["id"], "duration_s": sum(r.get("duration_s", 0.0) for r in recordings[:1]),
                # per point: time, altitude, ground speed, climb rate (the last two for the chart's read-out)
                "points": [[s["t"], s["alt_m"], s.get("speed_kmh"), s.get("vario_ms")] for s in thin(samples)],
                "scenes": [{"id": s["id"], "type": s["type"], "start_s": s["start_s"], "end_s": s["end_s"],
                            "attrs": s.get("attrs", {}), "thumb": (out / "review" / f"{s['id']}.jpg").exists()}
                           for s in scenes.get("scenes", [])],
                "shots": [{k: s[k] for k in ("reel", "theme", "type", "start_s", "duration_s")} for s in shots]}

    @app.post("/api/window/show")
    def window_show():
        """A second start of the app asks the running one to bring its window to the front."""
        show = getattr(app.state, "show_window", None)
        if show is None:
            raise HTTPException(404, "Aplikacja działa bez okna")
        show()
        return {"ok": True}

    def ai_key_present() -> bool:
        from core import aiframe
        try:
            return bool(aiframe.api_key())
        except aiframe.Unavailable:
            return False

    @app.get("/api/status")
    def status():
        """The overview's "Stan" cell: what this PC has to make films with."""
        s = settings()
        cache = Path(s["work"]) if s["work"] else data
        try:
            free = shutil.disk_usage(cache if cache.exists() else cache.anchor).free
        except OSError:
            free = None
        people = read_json(people_file)
        return {"stitcher": stitcher() is not None, "gpu": gpu_name(),
                "cache": {"path": s["work"], "free_bytes": free},
                "people": {"present": people is not None, "count": len([n for n in names_of(people) if n])},
                "ai_key": ai_key_present()}

    @app.get("/api/jobs/{job_id}/results")
    def results(job_id: int):
        out = out_dir(need(job_id))

        def info(p: Path) -> dict:
            stat = p.stat()
            return {"name": p.name, "size": stat.st_size, "mtime": stat.st_mtime}

        films, named = films_in(out)
        shots = [{"folder": d.name, "path": str(d), **named(d.name[6:]), "files": [info(f) for f in sorted(d.glob("*.mp4"))]}
                 for d in sorted(out.glob("shots_*")) if d.is_dir()]
        reels = (read_json(out / "reels.json") or {}).get("reels", [])
        return {"films": films, "shots": [s for s in shots if s["files"]],
                "skipped": [{**r, "title": named(str(r.get("tag", "")))["title"]} for r in reels
                            if isinstance(r, dict) and r.get("status") != "planned"],
                "report": (out / "scenes_report.txt").exists()}

    @app.get("/api/jobs/{job_id}/files/{path:path}")
    def file(job_id: int, path: str, download: bool = False):
        """A file from the job's output folder. Videos are sent with HTTP range support."""
        target = inside(out_dir(need(job_id)), path)
        if not target.is_file():
            raise HTTPException(404, "Nie ma takiego pliku")
        return FileResponse(target, filename=target.name if download else None)

    @app.get("/api/jobs/{job_id}/zip/{folder}")
    def shots_zip(job_id: int, folder: str):
        job = need(job_id)
        target = inside(out_dir(job), folder)
        if not folder.startswith("shots_") or not target.is_dir():
            raise HTTPException(404, "Nie ma takich ujęć")
        name = re.sub(r"[^\w\-]+", "_", f"{Path(job['folder']).name}_{folder}", flags=re.ASCII).strip("_")
        return StreamingResponse(zip_stream(sorted(target.glob("*.mp4"))), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    # --- UI: scene review -------------------------------------------------
    @app.get("/api/jobs/{job_id}/review")
    def review(job_id: int):
        job = need(job_id)
        out = out_dir(job)
        scenes, reels = read_json(out / "scenes.json"), read_json(out / "reels.json")
        if scenes is None or reels is None:
            raise HTTPException(409, "Zadanie nie ma jeszcze planu (scenes.json, reels.json)")
        profile, mount = job_config(job)
        listed = []
        for entry in reels.get("reels", []):
            known = entry.get("status") == "planned" or entry.get("reason") == "removed by hand"
            plan = read_json(out / f"reel_plan_{entry['tag']}.json") if known else None
            listed.append({**entry, "plan": plan})
        return {"job": view(job, 12), "reels": listed,
                "scenes": [{**s, "thumb": (out / "review" / f"{s['id']}.jpg").exists(),
                            "default": default_shot(s, profile, mount)} for s in scenes.get("scenes", [])],
                "presets": list(mount.presets) if mount else [],
                "projections": {name: p.projection for name, p in mount.presets.items()} if mount else {},
                "safe_zone": profile.safe_zone.model_dump() if profile else None,
                "aspect": [profile.output.width, profile.output.height] if profile else [9, 16]}

    @app.put("/api/jobs/{job_id}/review")
    def save_review(job_id: int, body: ReviewEdit):
        """Write the edited reel plans with "edited": true; `plan` then keeps them."""
        job = need(job_id)
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "Zadanie jest w kolejce albo właśnie się wykonuje")
        out = out_dir(job)
        scenes = {s["id"]: s for s in (read_json(out / "scenes.json") or {}).get("scenes", [])}
        planned = {r["tag"] for r in (read_json(out / "reels.json") or {}).get("reels", [])
                   if r.get("status") == "planned" or r.get("reason") == "removed by hand"}
        _, mount = job_config(job)
        plans = {}
        for tag, shots in body.reels.items():
            plan = read_json(out / f"reel_plan_{tag}.json") if tag in planned else None
            if plan is None:
                raise HTTPException(404, f"Nie ma rolki '{tag}'")
            if not shots:                           # "Usuń rolkę": its plan stays as it was, it is just not made
                plan.update(removed=True, edited=True)
                plans[tag] = plan
                continue
            old = {(s["scene_id"], s["start_s"], s["duration_s"]) for s in plan["shots"]}
            fixed = []
            for index, shot in enumerate(shots, 1):
                scene = scenes.get(shot.get("scene_id"))
                try:
                    start, length = float(shot["start_s"]), float(shot["duration_s"])
                    speed = float(shot.get("speed") or 1.0)
                    framing = [str(name) for name in shot["framing"]]
                except (KeyError, TypeError, ValueError):
                    raise HTTPException(422, f"Rolka {tag}, ujęcie {index}: brak danych")
                if scene is None:
                    raise HTTPException(422, f"Rolka {tag}: nie ma sceny '{shot.get('scene_id')}'")
                own = (shot.get("attrs") or {}).get("views") or {}        # views an AI made for this shot
                if not framing or (mount and any(name not in mount.presets and name not in own for name in framing)):
                    raise HTTPException(422, f"Rolka {tag}, ujęcie {index}: nieznany preset kadru")
                kinds = {(own[name].get("projection", "flat") if name in own else mount.presets[name].projection)
                         for name in framing} if mount else set()
                if len(kinds) > 1:
                    raise HTTPException(422, f"Rolka {tag}, ujęcie {index}: kadry mają różne projekcje "
                                             "(zwykła, planeta, rybie oko). W jednym ujęciu wszystkie muszą mieć tę samą.")
                if (scene["id"], start, length) not in old:         # a changed shot stays inside its scene
                    length = round(min(max(length, speed), scene["end_s"] - scene["start_s"]), 2)
                    start = round(min(max(start, scene["start_s"]), scene["end_s"] - length), 2)
                lower = [str(n) for n in shot.get("second") or [] if not mount or n in mount.presets or n in own]
                shot = {**shot, "second": lower}
                fixed.append({**shot, "index": index, "start_s": start, "duration_s": length, "speed": speed,
                              "framing": framing, "recording": scene.get("recording", "")})
            plan.update(shots=fixed, edited=True, removed=False,
                        length_s=round(sum(s["duration_s"] / s["speed"] for s in fixed), 2))
            try:
                from core.contracts import ReelPlan
                ReelPlan.model_validate(plan)
            except ValidationError as error:
                raise HTTPException(422, str(error))
            plans[tag] = plan
        for tag, plan in plans.items():
            (out / f"reel_plan_{tag}.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), "utf-8")
        return {tag: plan["length_s"] for tag, plan in plans.items()}

    preview_lock = threading.Lock()

    @app.post("/api/jobs/{job_id}/preview")
    def preview(job_id: int, body: PreviewAsk):
        """Make the pictures that are not there yet (from the proxy) and say where each one is."""
        job = need(job_id)
        review_dir = out_dir(job) / "review"
        if not (out_dir(job) / "scenes.json").exists():
            raise HTTPException(409, "Zadanie nie ma jeszcze planu")

        def named(kind: str, payload, ext: str) -> str:
            digest = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
            return f"{kind}/{digest}.{ext}"

        def aim(attrs: dict) -> dict:               # what of a shot decides where a view looks
            return {k: v for k, v in (attrs or {}).items() if k == "views" or k.startswith(("yaw_", "pitch_", "track_"))}

        spec: dict = {"stills": [], "clips": []}
        stills = []
        try:
            for item in body.stills:
                shot = item["shot"]
                at, preset = round(float(item["at"]), 1), str(item["preset"])
                base = {"scene_id": str(shot["scene_id"]), "recording": str(shot.get("recording") or ""),
                        "attrs": aim(shot.get("attrs")), "start_s": at, "duration_s": 1.0}
                file = named("stills", [base, preset], "jpg")
                stills.append(file)
                if not (review_dir / file).exists() and not any(s["file"] == file for s in spec["stills"]):
                    spec["stills"].append({"file": file, "shot": base, "at": at, "preset": preset})
            clip = None
            if body.clip:
                c = body.clip
                shot = {"scene_id": str(c["scene_id"]), "type": str(c.get("type") or ""), "start_s": float(c["start_s"]),
                        "duration_s": float(c["duration_s"]), "framing": [str(n) for n in c["framing"]],
                        "recording": str(c.get("recording") or ""), "speed": float(c.get("speed") or 1.0),
                        "attrs": aim(c.get("attrs"))}
                clip = named("clips", shot, "mp4")
                if not (review_dir / clip).exists():
                    spec["clips"].append({"file": clip, "shot": shot})
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "Podgląd: brak danych ujęcia")
        said: list[str] = []
        if spec["stills"] or spec["clips"]:
            review_dir.mkdir(parents=True, exist_ok=True)
            wanted = review_dir / f"preview_{uuid.uuid4().hex}.json"
            wanted.write_text(json.dumps(spec), "utf-8")
            work = job_work(settings()["work"], job["id"])
            cmd = [python_exe(), "-m", *cli_args("preview", job["folder"], job["options"], work)]
            try:
                with preview_lock:                  # one at a time: they all read the same proxy
                    runner(cmd, said.append, lambda: False, {"INSTA_ROLLER_PREVIEW": str(wanted)})
            finally:
                wanted.unlink(missing_ok=True)
        problems = [line for line in said if line.startswith("preview: ") and "->" not in line]
        return {"stills": [file if (review_dir / file).exists() else None for file in stills],
                "clip": clip if clip and (review_dir / clip).exists() else None,
                "error": problems[-1] if problems else ""}

    # --- UI: settings -----------------------------------------------------
    @app.get("/api/settings")
    def settings_get():
        return {**settings(), "data": str(data), "profiles": str(profiles)}

    def effective(name: str, overrides: dict | None = None) -> dict:
        """A profile as a job will get it: its file with the form's changes laid over."""
        path = config_path("profiles", name)
        if not path.exists():
            raise HTTPException(404, "Nie ma takiego zestawu ustawień")
        data = yaml.safe_load(path.read_text("utf-8"))
        return merge(data, json.loads(json.dumps((settings()["overrides"] if overrides is None else overrides).get(name) or {})))

    @app.get("/api/config/profiles/{name}/form")
    def profile_form(name: str):
        """For the settings form: the profile's own values, the values in force, and its reel themes."""
        if not NAME.fullmatch(name):
            raise HTTPException(404)
        base = yaml.safe_load(config_path("profiles", name).read_text("utf-8")) if config_path("profiles", name).exists() else None
        if base is None:
            raise HTTPException(404, "Nie ma takiego zestawu ustawień")
        themes = []
        for theme in base.get("themes", []):
            try:
                t = load_theme(theme, profiles)
                themes.append({"name": t.name, "title": t.title or t.name, "length": t.target_length_s or base.get("target_length_s")})
            except Exception:
                pass
        values = Profile.model_validate(effective(name)).model_dump()
        return {"base": Profile.model_validate(base).model_dump(), "values": values, "themes": themes,
                "changed": bool(settings()["overrides"].get(name))}

    @app.put("/api/settings")
    def settings_put(body: Settings):
        """The cache folder is created here, so a path that cannot be used is refused at once."""
        new = {**settings(), **body.model_dump(exclude_unset=True)}
        for name in new["overrides"]:               # a change that makes a profile unusable is refused here, not at render time
            if not NAME.fullmatch(name):
                raise HTTPException(422, "Zła nazwa zestawu ustawień")
            try:
                Profile.model_validate(effective(name, new["overrides"]))
            except ValidationError as error:
                first = error.errors()[0]
                raise HTTPException(422, f"Ustawienie {'.'.join(str(x) for x in first['loc'])}: {first['msg']}")
        new["overrides"] = {k: v for k, v in new["overrides"].items() if v}
        new["lengths"] = {k: v for k, v in new["lengths"].items() if NAME.fullmatch(k) and v and v >= 5}
        work = new["work"].strip()
        if work:
            work = str(absolute(work))
            try:
                Path(work).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise HTTPException(422, f"Nie można utworzyć folderu: {error.strerror or error}")
        data.mkdir(parents=True, exist_ok=True)
        save({**new, "work": work})
        return settings_get()

    @app.post("/api/config/compose")
    def compose(body: Compose):
        """Profile "inne": an existing profile with the form's choices applied, as YAML to edit further."""
        path = config_path("profiles", body.base)
        if not path.exists():
            raise HTTPException(404, "Nie ma takiego profilu")
        data = merge(yaml.safe_load(path.read_text("utf-8")), body.changes)
        return {"text": yaml.safe_dump(data, sort_keys=False, allow_unicode=True)}

    @app.post("/api/config/{kind}/validate")
    def config_validate(kind: str, body: Text):
        if kind not in KINDS:
            raise HTTPException(404)
        return {"ok": True, "name": parse(kind, body.text).name}

    @app.get("/api/config/{kind}/{name}")
    def config_get(kind: str, name: str):
        path = config_path(kind, name)
        if not path.exists():
            raise HTTPException(404, "Nie ma takiego pliku")
        return {"text": path.read_text("utf-8")}

    @app.put("/api/config/{kind}/{name}")
    def config_put(kind: str, name: str, body: Text):
        path = config_path(kind, name)
        parse(kind, body.text, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.text, encoding="utf-8")
        return {"ok": True, "name": name}

    people_page.add_routes(app, people_file, Path(env_file or REPO / ".env"), profiles, runner, settings, save)

    # --- the UI itself ----------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", FreshFiles(directory=STATIC), name="static")
    return app

"""The local web server: the API for the browser UI, and the UI itself.

It never runs FFmpeg or the pipeline (app.runner does, as CLI subprocesses). It keeps the
queue, edits YAML and JSON files, and serves what the steps wrote into a job's output folder.
It writes only there, into profiles/ and into the app's data folder. There is no login.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.config import PROFILES_DIR, Mount, Profile, Theme, list_names, load_mount, load_profile

from .db import Busy, Queue
from .steps import JOB_PROFILE, OUT_DIR, cli_args, job_work, pipeline, render_steps
from .steps import out_dir as job_out

STATIC = Path(__file__).parent / "static"
REPO = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[A-Za-z0-9_\-]+")
KINDS = {"profiles": ("", Profile), "mounts": ("mounts", Mount), "themes": ("themes", Theme)}
VIDEO = {".insv", ".lrv", ".mp4"}


class JobOptions(BaseModel):
    """What the "Nowe zadanie" form sends; every field maps to a CLI option (app.steps.cli_args)."""
    model_config = ConfigDict(extra="forbid")
    out: str = ""                           # "folder wyników"; empty = <job folder>/insta-roller
    profile: str = "auto"
    profile_yaml: str = ""                  # profile "inne": the job's own settings
    mount: str = ""
    length: float | None = Field(None, gt=0)
    enable: list[str] = []                  # themes switched on for this job, e.g. landing
    disable: list[str] = []
    mode: Literal["day", "trip", "both"] = "day"
    people: list[str] = []
    overlay: bool | None = None
    review: bool = False                    # stop after analysis + plan for the scene review
    shots: bool = False                     # also export the separate shots
    source: Literal["auto", "proxy", "sdk"] = "auto"


class NewJob(BaseModel):
    folder: str
    options: JobOptions = JobOptions()


class Settings(BaseModel):
    """The app's own settings, kept in <data>/settings.json."""
    model_config = ConfigDict(extra="forbid")
    work: str = ""                          # local cache folder (--work); empty = <output folder>/.cache


class Text(BaseModel):
    text: str


class Compose(BaseModel):
    base: str
    changes: dict


class Version(BaseModel):
    length: float = Field(gt=0)


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
               people: str | Path | None = None) -> FastAPI:
    """`data`: the app's folder (queue.db, settings.json). `profiles`: the profiles the CLI uses,
    edited in place. `people`: the people.json whose names the form offers."""
    data = Path(data or default_data_dir())
    profiles = Path(profiles or PROFILES_DIR)
    people_file = Path(people or REPO / "people.json")
    queue = Queue(data / "queue.db")
    app = FastAPI(title="insta-roller", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.queue, app.state.data, app.state.profiles = queue, data, profiles

    # --- helpers ----------------------------------------------------------
    def settings() -> dict:
        try:
            return Settings.model_validate(read_json(data / "settings.json") or {}).model_dump()
        except ValidationError:
            return Settings().model_dump()

    app.state.work = lambda: settings()["work"]         # for the job runner

    def need(job_id: int) -> dict:
        job = queue.get(job_id)
        if job is None:
            raise HTTPException(404, "Nie ma takiego zadania")
        return job

    def out_dir(job: dict) -> Path:
        return Path(job_out(job["folder"], job["options"]))

    def view(job: dict, log_lines: int | None = None) -> dict:
        job = dict(job)
        if log_lines:
            job["log"] = "\n".join(job["log"].splitlines()[-log_lines:])
        work = job_work(settings()["work"], job["id"])
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
                    "videos": 0, "igc": 0, "processed": False, "job": None, "out": ""}
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
                                             "people": list(p.people)})
        people = names_of(read_json(people_file))
        for p in found:
            people += [n for n in p.get("people", []) if n not in people]
        return {"profiles": found, "people": [n for n in people if n],
                "mounts": each("mounts", lambda m: {"presets": list(m.presets)}),
                "themes": each("themes", lambda t: {"title": t.title, "enabled": t.enabled,
                                                    "length": t.target_length_s})}

    # --- UI: jobs ---------------------------------------------------------
    @app.get("/api/jobs")
    def jobs():
        return [view(j, 12) for j in queue.list()]

    @app.post("/api/jobs")
    def create(body: NewJob):
        folder = absolute(body.folder)
        if not folder.is_dir():
            raise HTTPException(404, "Nie ma takiego folderu")
        options = body.options.model_dump()
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
    def render(job_id: int):
        """"Renderuj" after the scene review: plan (keeps the edited reel plans) + render."""
        return rerender(job_id)

    # --- UI: results ------------------------------------------------------
    @app.get("/api/jobs/{job_id}/results")
    def results(job_id: int):
        out = out_dir(need(job_id))

        def info(p: Path) -> dict:
            stat = p.stat()
            return {"name": p.name, "size": stat.st_size, "mtime": stat.st_mtime}

        films = sorted(map(info, out.glob("reel_*.mp4")), key=lambda f: -f["mtime"])
        shots = [{"folder": d.name, "files": [info(f) for f in sorted(d.glob("*.mp4"))]}
                 for d in sorted(out.glob("shots_*")) if d.is_dir()]
        reels = (read_json(out / "reels.json") or {}).get("reels", [])
        return {"films": films, "shots": [s for s in shots if s["files"]],
                "skipped": [r for r in reels if isinstance(r, dict) and r.get("status") != "planned"],
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
            plan = read_json(out / f"reel_plan_{entry['tag']}.json") if entry.get("status") == "planned" else None
            listed.append({**entry, "plan": plan})
        return {"job": view(job, 12), "reels": listed,
                "scenes": [{**s, "thumb": (out / "review" / f"{s['id']}.jpg").exists(),
                            "default": default_shot(s, profile, mount)} for s in scenes.get("scenes", [])],
                "presets": list(mount.presets) if mount else [],
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
                   if r.get("status") == "planned"}
        _, mount = job_config(job)
        plans = {}
        for tag, shots in body.reels.items():
            plan = read_json(out / f"reel_plan_{tag}.json") if tag in planned else None
            if plan is None:
                raise HTTPException(404, f"Nie ma rolki '{tag}'")
            if not shots:
                raise HTTPException(422, f"Rolka {tag} nie może być pusta; wyłącz jej temat w zadaniu")
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
                if not framing or (mount and any(name not in mount.presets for name in framing)):
                    raise HTTPException(422, f"Rolka {tag}, ujęcie {index}: nieznany preset kadru")
                if (scene["id"], start, length) not in old:         # a changed shot stays inside its scene
                    length = round(min(max(length, speed), scene["end_s"] - scene["start_s"]), 2)
                    start = round(min(max(start, scene["start_s"]), scene["end_s"] - length), 2)
                fixed.append({**shot, "index": index, "start_s": start, "duration_s": length, "speed": speed,
                              "framing": framing, "recording": scene.get("recording", "")})
            plan.update(shots=fixed, edited=True,
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

    # --- UI: settings -----------------------------------------------------
    @app.get("/api/settings")
    def settings_get():
        return {**settings(), "data": str(data), "profiles": str(profiles)}

    @app.put("/api/settings")
    def settings_put(body: Settings):
        """The cache folder is created here, so a path that cannot be used is refused at once."""
        work = body.work.strip()
        if work:
            work = str(absolute(work))
            try:
                Path(work).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise HTTPException(422, f"Nie można utworzyć folderu: {error.strerror or error}")
        data.mkdir(parents=True, exist_ok=True)
        (data / "settings.json").write_text(json.dumps({"work": work}, indent=2), encoding="utf-8")
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

    # --- the UI itself ----------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", FreshFiles(directory=STATIC), name="static")
    return app

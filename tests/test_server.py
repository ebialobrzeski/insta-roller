"""Stage 7: the standalone app (queue, web API, job runner), without FFmpeg or footage: the CLI runner is faked."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient      # noqa: E402

from app.__main__ import free_port, lock       # noqa: E402
from app.runner import REPO, Runner, run_subprocess     # noqa: E402
from app.steps import cli_args                 # noqa: E402
from app.web import absolute, create_app       # noqa: E402
from core.config import PROFILES_DIR           # noqa: E402


class Env:
    """A temp "disk" with two job folders, the app on its own data folder, and a browser (TestClient)."""

    def __init__(self, tmp_path: Path):
        self.tmp, self.root, self.data = tmp_path, tmp_path / "root", tmp_path / "data"
        self.lot1, self.lot2 = self.root / "loty" / "lot1", self.root / "loty" / "lot2"
        self.lot1.mkdir(parents=True)
        (self.lot1 / "lot.igc").write_text("x")
        self.lot2.mkdir()
        (tmp_path / "secret.txt").write_text("outside every output folder")
        self.profiles = shutil.copytree(PROFILES_DIR, tmp_path / "profiles")
        self.people = tmp_path / "people.json"
        self.start()

    def start(self) -> None:
        """Start the app; again = the app was closed and started on the same data folder."""
        self.app = create_app(self.data, self.profiles, self.people)
        self.queue, self.client = self.app.state.queue, TestClient(self.app)

    def submit(self, folder: Path | str | None = None, **options) -> dict:
        r = self.client.post("/api/jobs", json={"folder": str(folder or self.lot1), "options": options})
        assert r.status_code == 200, r.text
        return r.json()

    def job(self, job_id: int = 1) -> dict:
        return self.client.get(f"/api/jobs/{job_id}").json()

    def runner(self, fake, **kwargs) -> Runner:
        return Runner(self.queue, runner=fake, work=self.app.state.work, interval_s=0, **kwargs)

    def finish(self, status: str = "done") -> None:
        """What the runner does to the job at the head of the queue, without running anything."""
        self.queue.finish(self.queue.claim()["id"], status)


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def fill_output(out: Path) -> Path:
    """What analyze + plan leave behind, cut down to two reels and three scenes."""
    out.mkdir(parents=True, exist_ok=True)
    scenes = [{"id": "000_takeoff", "type": "takeoff", "start_s": 100.0, "end_s": 125.0, "score": 1.0, "attrs": {}, "recording": ""},
              {"id": "001_thermal", "type": "thermal", "start_s": 300.0, "end_s": 400.0, "score": 0.8,
               "attrs": {"direction": "right"}, "recording": ""},
              {"id": "002_glide", "type": "glide", "start_s": 500.0, "end_s": 560.0, "score": 0.5, "attrs": {}, "recording": ""}]
    info = {"id": "rec", "kind": "dual_fisheye", "files": ["a.lrv"], "duration_s": 900.0}
    (out / "scenes.json").write_text(json.dumps({
        "version": 1, "fingerprint": "f", "recording": info, "recordings": [info], "source": "igc", "missing_phases": [],
        "sync": {"video_start_utc": 0.0, "offset_s": 0.0, "method": "test"}, "scenes": scenes}))
    (out / "reels.json").write_text(json.dumps({"version": 1, "reels": [
        {"theme": "thermal", "tag": "thermal_30s", "status": "planned", "length_s": 8.0, "shots": 1},
        {"theme": "flight_summary", "tag": "flight_summary_45s", "status": "planned", "length_s": 5.0, "shots": 1},
        {"theme": "landing", "tag": "landing_20s", "status": "skipped", "reason": "theme is switched off"}]}))

    def plan(theme, target, shots):
        return {"version": 1, "theme": theme, "target_length_s": target, "edited": False, "title": "",
                "length_s": sum(s["duration_s"] for s in shots), "shots": shots}

    (out / "reel_plan_thermal_30s.json").write_text(json.dumps(plan("thermal", 30.0, [
        {"index": 1, "scene_id": "001_thermal", "type": "thermal", "start_s": 346.0, "duration_s": 8.0,
         "framing": ["wing_right", "back_right"], "recording": "", "speed": 1.0, "attrs": {}}])))
    (out / "reel_plan_flight_summary_45s.json").write_text(json.dumps(plan("flight_summary", 45.0, [
        {"index": 1, "scene_id": "000_takeoff", "type": "takeoff", "start_s": 100.0, "duration_s": 5.0,
         "framing": ["front"], "recording": "", "speed": 1.0, "attrs": {}}])))
    return out


# --- queue -------------------------------------------------------------------
def test_job_creation_and_cli_commands(env):
    job = env.submit(profile="flight", length=30, enable=["landing"], overlay=False, shots=True)
    assert (job["status"], job["steps"], job["final"]) == ("queued", ["analyze", "plan", "render", "shots"], "done")
    assert job["folder"] == str(env.lot1) and not job["existing"]
    assert job["out"] == str(env.lot1 / "insta-roller")
    assert job["commands"][0] == "python -m " + subprocess.list2cmdline(
        ["core.cli", "analyze", str(env.lot1), "--profile=flight", "--length=30", "--enable=landing", "--no-overlay"])
    review = env.submit(env.lot2, review=True)
    assert (review["steps"], review["final"]) == (["analyze", "plan", "thumbnails"], "review")
    post = env.client.post
    assert post("/api/jobs", json={"folder": str(env.root / "loty" / "none")}).status_code == 404
    assert post("/api/jobs", json={"folder": str(env.lot1 / "lot.igc")}).status_code == 404       # a file
    assert post("/api/jobs", json={"folder": str(env.lot1), "options": {"profile": "nope"}}).status_code == 422
    assert post("/api/jobs", json={"folder": str(env.lot1), "options": {"bogus": 1}}).status_code == 422
    assert not (env.lot1 / "insta-roller").exists()      # queueing writes nothing into the job folder


def test_same_folder_reuses_the_job_row(env):
    first = env.submit(profile="flight")
    other_spelling = str(env.lot1).replace("\\", "/").upper() + "/" if os.name == "nt" else f"{env.lot1}/../lot1/"
    again = env.submit(other_spelling, profile="flight", length=20)
    assert again["id"] == first["id"] and again["existing"] and again["options"]["length"] == 20
    assert again["folder"] == str(env.lot1) and len(env.client.get("/api/jobs").json()) == 1
    assert env.queue.claim()["id"] == first["id"]
    running = env.submit(length=99)                      # a running job is left alone
    assert running["existing"] and running["status"] == "running" and running["options"]["length"] == 20
    assert env.queue.claim() is None                     # one job at a time, nothing else waits


def test_progress_reporting(env):
    job = env.submit()
    env.queue.claim()
    assert env.queue.report(job["id"], 0, 0.1, ["analyze: 43 scenes"]) == {"cancel": False}
    env.queue.report(job["id"], 2, 0.8, ["  shot 3/9"])
    seen = env.job()
    assert (seen["status"], seen["stage"], seen["progress"]) == ("running", "render", 0.8)
    assert seen["log"].splitlines() == ["analyze: 43 scenes", "  shot 3/9"]
    assert env.queue.finish(job["id"], "done")["status"] == "done"
    assert env.queue.report(job["id"], 2, 0.9, []) is None           # not running any more
    seen = env.client.get("/api/jobs").json()[0]
    assert (seen["status"], seen["progress"], seen["stage"]) == ("done", 1.0, "")


def test_cancel_and_retry(env):
    job = env.submit()
    url = f"/api/jobs/{job['id']}"
    assert env.client.post(f"{url}/cancel").json()["status"] == "cancelled"
    assert env.queue.claim() is None
    assert env.client.post(f"{url}/retry").json()["status"] == "queued"
    env.queue.claim()
    assert env.client.post(f"{url}/retry").status_code == 409
    assert env.client.post(f"{url}/cancel").json()["status"] == "running"       # the runner sees it at its next report
    assert env.queue.report(job["id"], 0, 0.0, []) == {"cancel": True}
    env.queue.finish(job["id"], "cancelled")
    assert env.job()["status"] == "cancelled"
    assert env.client.get("/api/jobs/999").status_code == 404


# --- review, results, versions -------------------------------------------------
def test_review_edit_writes_an_edited_reel_plan(env):
    client = env.client
    job = env.submit(profile="flight", review=True)
    url = f"/api/jobs/{job['id']}"
    assert client.get(f"{url}/review").status_code == 409            # nothing planned yet
    out = fill_output(env.lot1 / "insta-roller")
    env.finish()
    assert env.job()["status"] == "review"

    data = client.get(f"{url}/review").json()
    assert [r["tag"] for r in data["reels"] if r["plan"]] == ["thermal_30s", "flight_summary_45s"]
    assert "front" in data["presets"] and data["safe_zone"]["bottom"] == 0.2
    glide = next(s for s in data["scenes"] if s["id"] == "002_glide")["default"]
    assert glide["framing"] == ["pilot_wide", "front"] and 500 <= glide["start_s"] <= 560 - glide["duration_s"]
    thermal = next(s for s in data["scenes"] if s["id"] == "001_thermal")["default"]
    assert thermal["framing"] == ["wing_right", "back_right"]        # *_inner follows the circling direction

    # Move the thermal shot to the summary with another preset and too long; add the glide to the thermal reel.
    moved = {**data["reels"][0]["plan"]["shots"][0], "framing": ["pilot"], "duration_s": 500.0}
    summary = data["reels"][1]["plan"]["shots"] + [moved]
    r = client.put(f"{url}/review", json={"reels": {"thermal_30s": [glide], "flight_summary_45s": summary}})
    assert r.status_code == 200, r.text
    plan = json.loads((out / "reel_plan_flight_summary_45s.json").read_text())
    assert plan["edited"] is True and [s["index"] for s in plan["shots"]] == [1, 2]
    assert plan["shots"][0]["start_s"] == 100.0 and plan["shots"][0]["duration_s"] == 5.0    # untouched shot
    assert (plan["shots"][1]["framing"], plan["shots"][1]["start_s"], plan["shots"][1]["duration_s"]) == (["pilot"], 300.0, 100.0)
    assert plan["length_s"] == 105.0 == r.json()["flight_summary_45s"]
    other = json.loads((out / "reel_plan_thermal_30s.json").read_text())
    assert other["edited"] is True and [s["scene_id"] for s in other["shots"]] == ["002_glide"]
    from core.contracts import ReelPlan
    ReelPlan.model_validate(plan)

    bad = {**glide, "framing": ["no_such_preset"]}
    assert client.put(f"{url}/review", json={"reels": {"thermal_30s": [bad]}}).status_code == 422
    assert client.put(f"{url}/review", json={"reels": {"thermal_30s": [{**glide, "scene_id": "x"}]}}).status_code == 422
    assert client.put(f"{url}/review", json={"reels": {"thermal_30s": []}}).status_code == 422
    assert client.put(f"{url}/review", json={"reels": {"landing_20s": [glide]}}).status_code == 404
    assert client.put(f"{url}/review", json={"reels": {"../../x": [glide]}}).status_code == 404

    rendering = client.post(f"{url}/render").json()                  # "Renderuj"
    assert (rendering["status"], rendering["steps"], rendering["final"]) == ("queued", ["plan", "render"], "done")
    assert client.put(f"{url}/review", json={"reels": {"thermal_30s": [glide]}}).status_code == 409


def test_new_version_only_plans_and_renders(env):
    client = env.client
    job = env.submit(profile="flight", shots=True)
    url = f"/api/jobs/{job['id']}"
    assert client.post(f"{url}/version", json={"length": 25}).status_code == 409     # no analysis yet
    fill_output(env.lot1 / "insta-roller")
    assert client.post(f"{url}/version", json={"length": 25}).status_code == 409     # still queued
    env.finish()
    version = client.post(f"{url}/version", json={"length": 25}).json()
    assert version["id"] == job["id"] and version["status"] == "queued"
    assert version["steps"] == ["plan", "render", "shots"] and version["options"]["length"] == 25
    assert all("--length=25" in c for c in version["commands"])
    assert client.post(f"{url}/version", json={"length": 0}).status_code == 422
    client.post(f"{url}/cancel")
    plain = {**version["options"], "shots": False}
    client.post("/api/jobs", json={"folder": str(env.lot1), "options": plain})
    client.post(f"{url}/cancel")
    assert client.post(f"{url}/version", json={"length": 15}).json()["steps"] == ["plan", "render"]


def test_results_range_and_zip(env):
    client = env.client
    job = env.submit()
    out = fill_output(env.lot1 / "insta-roller")
    film = bytes(range(256)) * 40
    (out / "reel_thermal_30s.mp4").write_bytes(film)
    (out / "shots_thermal_30s").mkdir()
    for name in ("01_thermal.mp4", "02_glide.mp4"):
        (out / "shots_thermal_30s" / name).write_bytes(name.encode() * 1000)
    url = f"/api/jobs/{job['id']}"
    results = client.get(f"{url}/results").json()
    assert [f["name"] for f in results["films"]] == ["reel_thermal_30s.mp4"]
    assert [f["name"] for f in results["shots"][0]["files"]] == ["01_thermal.mp4", "02_glide.mp4"]
    assert results["skipped"][0]["tag"] == "landing_20s"

    part = client.get(f"{url}/files/reel_thermal_30s.mp4", headers={"Range": "bytes=1000-1999"})
    assert part.status_code == 206 and part.content == film[1000:2000]
    assert part.headers["content-range"] == f"bytes 1000-1999/{len(film)}"
    whole = client.get(f"{url}/files/reel_thermal_30s.mp4?download=1")
    assert whole.content == film and "attachment" in whole.headers["content-disposition"]

    packed = client.get(f"{url}/zip/shots_thermal_30s")
    assert packed.status_code == 200 and "lot1_shots_thermal_30s.zip" in packed.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(packed.content)) as archive:
        assert archive.testzip() is None and archive.namelist() == ["01_thermal.mp4", "02_glide.mp4"]
        assert archive.read("02_glide.mp4") == b"02_glide.mp4" * 1000
    assert client.get(f"{url}/zip/review").status_code == 404


# --- folders: the whole machine, absolute paths --------------------------------------
def test_folder_picker_lists_drives_and_any_absolute_folder(env):
    client = env.client
    top = client.get("/api/folders").json()
    assert top["path"] == "" and top["parent"] is None
    drive = Path(env.root.anchor)
    assert any(Path(d["path"]) == drive for d in top["dirs"])        # the temp folder's drive is offered
    here = client.get("/api/folders", params={"path": str(env.root)}).json()
    assert here["path"] == str(env.root) and here["parent"] == str(env.tmp)
    assert here["dirs"] == [{"name": "loty", "path": str(env.root / "loty")}]
    assert Path(here["crumbs"][0]["path"]) == drive and here["crumbs"][-1] == {"name": "root", "path": str(env.root)}
    assert client.get("/api/folders", params={"path": here["crumbs"][0]["path"]}).json()["parent"] == ""

    (env.lot1 / "a.insv").write_text("x")
    (env.lot1 / "insta-roller").mkdir()                              # the output folder is not offered as a job folder
    (env.lot1 / "$RECYCLE.BIN").mkdir()
    job = env.submit()
    inside = client.get("/api/folders", params={"path": f'"{env.lot1}{os.sep}"'}).json()      # pasted "as path"
    assert (inside["path"], inside["videos"], inside["igc"], inside["dirs"]) == (str(env.lot1), 1, 1, [])
    assert inside["processed"] and inside["job"] == job["id"] and inside["out"] == str(env.lot1 / "insta-roller")

    for path in ("loty/lot1", "..", "~", "lot1\\..", "C:relative"):                # not full paths
        assert client.get("/api/folders", params={"path": path}).status_code == 400, path
        assert client.post("/api/jobs", json={"folder": path}).status_code == 400, path
    assert client.get("/api/folders", params={"path": str(env.root / "none")}).status_code == 404
    assert client.get("/api/folders", params={"path": str(env.lot1 / "lot.igc")}).status_code == 404
    if os.name == "nt":                                              # UNC shares are full paths too
        assert absolute(r"\\nas\wideo\loty\..\lot") == Path(r"\\nas\wideo\lot")
        assert absolute("d:/Wideo/lot/") == Path("d:\\Wideo\\lot")


def test_only_the_output_folder_is_served_and_written(env):
    client = env.client
    job = env.submit()
    fill_output(env.lot1 / "insta-roller")
    url = f"/api/jobs/{job['id']}"
    assert client.get(f"{url}/files/scenes.json").status_code == 200
    for path in ("..%2F..%2F..%2F..%2Fsecret.txt", "..%2Flot.igc", "%2E%2E/%2E%2E/%2E%2E/%2E%2E/secret.txt", "C:%5CWindows%5Cwin.ini"):
        assert client.get(f"{url}/files/{path}").status_code in (400, 404), path
    assert client.get(f"{url}/files/..%2F..%2F..%2F..%2Fsecret.txt").status_code == 400
    assert client.get(f"{url}/files/..%2Flot.igc").status_code == 400            # recordings are not served
    assert client.get(f"{url}/zip/..%2F..").status_code in (400, 404)
    assert client.get("/api/config/profiles/..%2F..%2Fpyproject").status_code in (400, 404)
    assert client.put("/api/config/profiles/..%5Cx", json={"text": "name: x"}).status_code in (400, 404)
    assert sorted(p.name for p in env.lot1.iterdir()) == ["insta-roller", "lot.igc"]


def test_custom_output_folder_goes_to_the_cli_as_out(env):
    client = env.client
    out = env.tmp / "wyniki" / "lot1"
    text = (env.profiles / "flight.yaml").read_text("utf-8").replace("name: flight", "name: one_off", 1)
    job = env.submit(out=str(out), profile_yaml=text, review=True)
    assert job["options"]["out"] == str(out) == job["out"] and out.is_dir()
    assert (out / "job_profile.yaml").read_text("utf-8") == text and not (env.lot1 / "insta-roller").exists()
    assert f"--out={out}" in job["commands"][0] and f"--profile={out / 'job_profile.yaml'}" in job["commands"][0]

    fake = FakeCli()
    env.runner(fake, python="py").run_once()
    assert fake.last == ["py", "-u", "-m", "app.thumbs", str(env.lot1), f"--out={out}", f"--profile={out / 'job_profile.yaml'}"]
    fill_output(out)                                                 # review and results read the chosen folder
    (out / "reel_thermal_30s.mp4").write_bytes(b"film")
    url = f"/api/jobs/{job['id']}"
    assert len(client.get(f"{url}/review").json()["scenes"]) == 3
    assert [f["name"] for f in client.get(f"{url}/results").json()["films"]] == ["reel_thermal_30s.mp4"]
    assert client.get(f"{url}/files/reel_thermal_30s.mp4").content == b"film"
    assert client.get(f"{url}/files/..%2F..%2Fsecret.txt").status_code == 400
    assert f"--out={out}" in client.post(f"{url}/render").json()["commands"][1]     # the choice stays with the job

    def refused(folder, where):
        return client.post("/api/jobs", json={"folder": str(folder), "options": {"out": str(where)}}).status_code

    assert refused(env.lot2, env.lot2) == 422                        # films among the recordings
    assert refused(env.lot2, out) == 422                             # another job's results
    assert refused(env.lot2, "wyniki") == 400                        # not a full path
    assert refused(env.lot2, env.lot1 / "lot.igc") == 422            # cannot be created
    assert env.submit(env.lot2, out=str(env.lot2 / "insta-roller"))["options"]["out"] == ""     # the default


def test_cache_folder_setting_goes_to_the_cli_as_work(env):
    client = env.client
    assert client.get("/api/settings").json() == {"work": "", "data": str(env.data), "profiles": str(env.profiles)}
    work = env.tmp / "cache"
    assert client.put("/api/settings", json={"work": "cache"}).status_code == 400
    assert client.put("/api/settings", json={"work": str(env.lot1 / "lot.igc" / "x")}).status_code == 422
    assert client.put("/api/settings", json={"work": str(work)}).json()["work"] == str(work) and work.is_dir()
    assert json.loads((env.data / "settings.json").read_text())["work"] == str(work)
    job = env.submit(profile="flight")
    assert job["commands"][0].endswith(f"--work={work / 'job_1'}")
    fake = FakeCli()
    env.runner(fake, python="py").run_once()
    assert fake.last[-1] == f"--work={work / 'job_1'}"
    env.start()                                                      # settings outlive the app
    assert client.get("/api/settings").json()["work"] == str(work)
    assert env.client.put("/api/settings", json={"work": ""}).json()["work"] == ""
    assert "--work" not in env.job()["commands"][0]


# --- settings ------------------------------------------------------------------
def test_profile_save_validates_yaml(env):
    client, profiles = env.client, env.profiles
    meta = client.get("/api/meta").json()
    assert {"flight", "flight_motor"} <= {p["name"] for p in meta["profiles"]}
    assert next(t for t in meta["themes"] if t["name"] == "landing")["enabled"] is False
    text = client.get("/api/config/profiles/flight").json()["text"]
    assert text == (profiles / "flight.yaml").read_text("utf-8")

    def put(name, body, kind="profiles"):
        return client.put(f"/api/config/{kind}/{name}", json={"text": body})

    assert put("flight", "name: [unclosed").status_code == 422                       # not YAML
    assert put("flight", "name: flight\n").status_code == 422                        # fields missing
    assert put("flight", text + "\nno_such_setting: 1\n").status_code == 422         # unknown field
    assert put("flight", text.replace("mount: glider_panel", "mount: nowhere")).status_code == 422
    assert put("other", text).status_code == 422                                     # name differs from the file
    assert put("bad name!", text).status_code in (400, 404)
    assert (profiles / "flight.yaml").read_text("utf-8") == text                     # nothing was written

    changed = text.replace("target_length_s: 45", "target_length_s: 33")
    assert put("flight", changed).status_code == 200
    assert (profiles / "flight.yaml").read_text("utf-8") == changed                  # edited in place
    mine = changed.replace("name: flight", "name: my_trip", 1)                       # "zapisz jako nowy profil"
    assert put("my_trip", mine).status_code == 200 and (profiles / "my_trip.yaml").exists()
    assert "my_trip" in [p["name"] for p in client.get("/api/meta").json()["profiles"]]

    theme = client.get("/api/config/themes/landing").json()["text"]
    assert put("landing", theme.replace("enabled: false", "enabled: maybe"), "themes").status_code == 422
    assert put("landing", theme.replace("enabled: false", "enabled: true"), "themes").status_code == 200
    assert client.post("/api/config/mounts/validate", json={"text": "name: m\npresets: {}"}).status_code == 422

    composed = client.post("/api/config/compose", json={
        "base": "flight", "changes": {"output": {"width": 1920, "height": 1080}, "audio": True}}).json()["text"]
    assert client.post("/api/config/profiles/validate", json={"text": composed}).json()["ok"]
    assert "width: 1920" in composed and "fps: 30" in composed


def test_custom_profile_travels_with_the_job(env):
    text = (env.profiles / "flight.yaml").read_text("utf-8").replace("name: flight", "name: one_off", 1)
    bad = {"folder": str(env.lot1), "options": {"profile_yaml": "nonsense"}}
    assert env.client.post("/api/jobs", json=bad).status_code == 422
    job = env.submit(profile_yaml=text)
    own = env.lot1 / "insta-roller" / "job_profile.yaml"
    assert own.read_text("utf-8") == text
    assert f"--profile={own}" in job["commands"][0]
    assert not (env.profiles / "one_off.yaml").exists()


def test_people_offered_from_people_json(env):
    env.people.write_text(json.dumps({"people": [{"name": "Ala"}, {"name": "Olek"}]}))
    assert env.client.get("/api/meta").json()["people"][:2] == ["Ala", "Olek"]       # then the profiles' people
    env.people.write_text(json.dumps({"Ala": {"weight": 1}}))
    assert env.client.get("/api/meta").json()["people"][:1] == ["Ala"]
    job = env.submit(people=["Ala", " Olek "], mode="both")
    assert job["commands"][0].endswith("--people=Ala,Olek --mode=both")


def test_ui_pages_are_served(env):
    page = env.client.get("/")
    assert page.status_code == 200 and "Nowe zadanie" in page.text and "Kolejka" in page.text
    assert "Folder wyników" in page.text and "Folder pamięci podręcznej" in page.text
    assert env.client.get("/static/app.js").status_code == 200 and env.client.get("/static/style.css").status_code == 200
    assert env.client.get("/static/app.js").headers["cache-control"] == "no-cache"      # an updated app, never old JS


# --- the commands must be ones the real CLI accepts ----------------------------------
def test_generated_commands_parse_with_the_real_cli(monkeypatch, tmp_path):
    cli = pytest.importorskip("core.cli")
    seen = []
    monkeypatch.setattr(cli, "Job", lambda args: args)
    for step in ("analyze", "plan", "render", "shots"):
        monkeypatch.setattr(cli, f"step_{step}", lambda args, step=step: seen.append((step, args)))
    options = {"profile": "flight", "mount": "glider_panel", "length": 25.0, "enable": ["landing"], "disable": ["thermal"],
               "mode": "both", "people": ["Ala", "Olek"], "overlay": False, "source": "proxy", "review": True, "shots": True,
               "out": str(tmp_path / "out")}
    for step in ("analyze", "plan", "render", "shots"):
        module, *args = cli_args(step, str(tmp_path), options, work=str(tmp_path / "work"))
        assert module == "core.cli"
        cli.main(args)
    assert [s for s, _ in seen] == ["analyze", "plan", "render", "shots"]
    args = seen[0][1]
    assert (args.profile, args.mount, args.length, args.enable, args.disable) == ("flight", "glider_panel", 25.0, "landing", "thermal")
    assert (args.mode, args.people, args.overlay, args.source) == ("both", "Ala,Olek", False, "proxy")
    assert (args.folder, args.out, args.work) == (str(tmp_path), str(tmp_path / "out"), str(tmp_path / "work"))
    own = os.path.join("Z:\\a", "insta-roller", "job_profile.yaml")
    assert cli_args("thumbnails", "Z:\\a", {"profile_yaml": "x"})[:3] == ["app.thumbs", "Z:\\a", f"--profile={own}"]


# --- the job runner ----------------------------------------------------------------
class FakeCli:
    """Stands in for the subprocess runner: records the commands, plays back output."""

    def __init__(self, fail: dict | None = None, die_at: str | None = None, at: dict | None = None):
        self.calls, self.fail, self.die_at, self.at = [], fail or {}, die_at, at or {}

    def __call__(self, cmd, on_line, poll) -> int:
        step = cmd[4] if cmd[3] == "core.cli" else "thumbnails"
        self.calls.append(step)
        self.last = cmd
        if step == self.die_at:
            self.die_at = None
            raise KeyboardInterrupt("the app was killed")
        if step in self.at:
            self.at.pop(step)()
        for i in (1, 2):
            on_line(f"  shot {i}/2  scene")
            if poll():
                return -1
        return self.fail.pop(step, 0)


def wait_for(condition, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_runner_runs_the_steps_in_order(env):
    job = env.submit(profile="flight", length=30, shots=True)
    fake = FakeCli()
    runner = env.runner(fake, python="py")
    assert runner.run_once() is True
    assert fake.calls == ["analyze", "plan", "render", "shots"]
    assert fake.last == ["py", "-u", "-m", "core.cli", "shots", str(env.lot1), "--profile=flight", "--length=30"]
    seen = env.job(job["id"])
    assert (seen["status"], seen["progress"], seen["step_index"]) == ("done", 1.0, 4)
    assert seen["log"].count("$ py -u -m core.cli") == 4 and "shot 2/2" in seen["log"]
    assert runner.run_once() is False                    # the queue is empty


def test_background_runner_takes_jobs_from_queued_to_done(env):
    """The app's own thread does the work: nothing else has to be started."""
    first = env.submit(profile="flight")
    fake = FakeCli()
    runner = env.runner(fake)
    runner.start()
    try:
        assert wait_for(lambda: env.job(first["id"])["status"] == "done")
        second = env.submit(env.lot2, review=True)       # queued while the app is running
        assert wait_for(lambda: env.job(second["id"])["status"] == "review")
        assert fake.calls == ["analyze", "plan", "render", "analyze", "plan", "thumbnails"]
    finally:
        runner.stop()
    assert not runner.thread.is_alive()


def test_job_resumes_at_its_step_after_the_app_restarts(env):
    job = env.submit(profile="flight", review=True)
    first = FakeCli(die_at="plan")
    with pytest.raises(KeyboardInterrupt):               # killed in the middle of "plan"
        env.runner(first).run_once()
    assert first.calls == ["analyze", "plan"]
    assert (env.job()["status"], env.job()["stage"]) == ("running", "plan")

    env.start()                                          # the app again, on the same data folder
    second = FakeCli(fail={"thumbnails": 1})
    runner = env.runner(second)
    runner.start()
    try:
        assert wait_for(lambda: env.job()["status"] == "review")
    finally:
        runner.stop()
    assert second.calls == ["plan", "thumbnails"]        # analyze is not repeated; failed thumbnails do not matter

    fill_output(env.lot1 / "insta-roller")
    env.client.post(f"/api/jobs/{job['id']}/render")
    third = FakeCli()
    env.runner(third).run_once()
    assert third.calls == ["plan", "render"] and env.job()["status"] == "done"


def test_closing_the_app_stops_the_step_and_keeps_the_job(env):
    env.submit(profile="flight")
    runner = env.runner(None)
    runner.runner = fake = FakeCli(at={"plan": runner.closing.set})      # Ctrl+C while "plan" runs
    runner.run_once()
    assert fake.calls == ["analyze", "plan"]
    assert (env.job()["status"], env.job()["error"]) == ("running", "")
    runner.stop()
    seen = env.job()
    assert (seen["status"], seen["stage"], seen["step_index"]) == ("queued", "plan", 1)
    assert "shot 1/2" in seen["log"]

    env.client.post("/api/jobs/1/cancel")                # cancelled while the app was closed: not resumed
    assert env.job()["status"] == "cancelled"
    again = env.runner(FakeCli())
    again.start()
    again.stop()
    assert again.runner.calls == [] and env.job()["status"] == "cancelled"


def test_runner_reports_failure_and_retry_continues_at_the_failed_step(env):
    job = env.submit(profile="flight")
    fake = FakeCli(fail={"render": 3})
    runner = env.runner(fake)
    runner.run_once()
    seen = env.job()
    assert seen["status"] == "error" and seen["error"].startswith("render: exit code 3") and seen["stage"] == "render"
    env.client.post(f"/api/jobs/{job['id']}/retry")
    runner.run_once()
    assert fake.calls == ["analyze", "plan", "render", "render"]
    assert env.job()["status"] == "done"

    def broken(cmd, on_line, poll):
        raise FileNotFoundError("no python here")

    env.submit(env.lot2)
    env.runner(broken).run_once()                        # a step that cannot even start is an error, not a crash
    assert env.job(2)["status"] == "error" and "no python here" in env.job(2)["error"]


def test_runner_stops_a_cancelled_job(env):
    env.submit(profile="flight")
    fake = FakeCli(at={"plan": lambda: env.client.post("/api/jobs/1/cancel")})
    env.runner(fake).run_once()
    assert fake.calls == ["analyze", "plan"]
    assert env.job()["status"] == "cancelled"


# --- real processes ------------------------------------------------------------------
def alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True)
        return f'"{pid}"' in listed.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    elif alive(pid):
        os.kill(pid, 9)


SLEEP = "import os, time; print(os.getpid(), flush=True); time.sleep(120)"
# A step that starts its own child, as the CLI starts FFmpeg.
STEP = ("import os, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', {SLEEP!r}]); "
        "print(os.getpid(), flush=True); time.sleep(120)")


def test_stopping_a_step_kills_its_whole_process_tree():
    pids: list[int] = []
    started = time.monotonic()
    try:
        code = run_subprocess([sys.executable, "-c", STEP], lambda line: pids.append(int(line)),
                              lambda: len(pids) == 2)      # stop once the step and its child are both up
        assert code == -1 and len(pids) == 2
        assert wait_for(lambda: not any(alive(pid) for pid in pids), 10), "a child process was left behind"
        assert time.monotonic() - started < 60
    finally:
        for pid in pids:
            kill(pid)


def test_a_finished_step_returns_its_exit_code_and_output():
    lines: list[str] = []
    code = run_subprocess([sys.executable, "-c", "import sys; print('zażółć'); sys.exit(3)"], lines.append, lambda: False)
    assert (code, lines) == (3, ["zażółć"])


@pytest.mark.skipif(os.name != "nt", reason="job objects are a Windows feature")
def test_a_killed_app_takes_its_step_with_it():
    """No Ctrl+C, no clean-up code: the app process is killed outright; the step must still not survive."""
    app = (f"import sys; from app.runner import run_subprocess; "
           f"run_subprocess([sys.executable, '-c', {SLEEP!r}], lambda line: print(line, flush=True), lambda: False)")
    outer = subprocess.Popen([sys.executable, "-c", app], stdout=subprocess.PIPE, text=True, cwd=REPO)
    pid = 0
    try:
        pid = int(outer.stdout.readline())
        assert alive(pid)
        outer.kill()
        outer.wait()
        assert wait_for(lambda: not alive(pid), 10), "the step outlived the app"
    finally:
        outer.kill()
        if pid:
            kill(pid)


# --- starting the app ------------------------------------------------------------------
def test_port_and_single_instance(tmp_path):
    import socket
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        other = free_port("127.0.0.1", port)             # the wanted port is in use: another free one
        assert other != port and other > 0
    assert free_port("127.0.0.1", other) == other
    held = lock(tmp_path / "data")
    assert held is not None and lock(tmp_path / "data") is None      # a second app on the same data folder
    held.close()
    again = lock(tmp_path / "data")
    assert again is not None
    again.close()

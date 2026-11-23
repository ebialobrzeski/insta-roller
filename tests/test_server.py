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

from app import web                            # noqa: E402
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
        self.people, self.env_file = tmp_path / "people.json", tmp_path / ".env"
        self.stitcher = None                    # the Media SDK stitcher is "not built" unless a test says so
        self.immich = FakeImmich()
        self.start()

    def start(self) -> None:
        """Start the app; again = the app was closed and started on the same data folder."""
        self.app = create_app(self.data, self.profiles, self.people, self.env_file,
                              stitcher=lambda: self.stitcher, runner=self.immich)
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


class FakeImmich:
    """Stands in for the subprocess runner of the Immich export: records the command and its environment."""

    def __init__(self):
        self.calls, self.code = [], 0

    def __call__(self, cmd, on_line, poll, env=None) -> int:
        self.calls.append((cmd, env))
        on_line(f"faces-export: asking {cmd[cmd.index('--immich-url') + 1]} with key {env['IMMICH_API_KEY']}")
        on_line("faces-export: 2 people" if self.code == 0 else "faces-export: Immich said 401")
        return self.code


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    for name in ("IMMICH_TAILSCALE_ADDRESS", "IMMICH_ADDRESS", "IMMICH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
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
    assert client.get("/api/settings").json() == {"work": "", "quality": "auto", "people_names": [],
                                                  "data": str(env.data), "profiles": str(env.profiles)}
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
    assert env.client.put("/api/settings", json={"quality": "good"}).json()["work"] == str(work)     # one setting at a time
    assert env.client.put("/api/settings", json={"quality": "nonsense"}).status_code == 422
    assert env.client.put("/api/settings", json={"work": ""}).json() == {
        "work": "", "quality": "good", "people_names": [], "data": str(env.data), "profiles": str(env.profiles)}
    assert "--work" not in env.job()["commands"][0] and env.client.get("/api/meta").json()["quality"] == "good"


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
    assert page.status_code == 200 and 'id="view"' in page.text and "/static/app.js" in page.text
    assert "http://" not in page.text and "https://" not in page.text                # works offline: nothing from the web
    files = {name: env.client.get(f"/static/{name}") for name in (
        "app.js", "ui.js", "live.js", "home.js", "wizard.js", "films.js", "review.js", "people.js", "settings.js", "style.css")}
    assert all(r.status_code == 200 and r.headers["cache-control"] == "no-cache" for r in files.values())   # never old JS
    assert all(text in files["app.js"].text for text in ("Przegląd", "Nowy film", "Filmy", "Osoby", "Ustawienia"))
    assert "Zrób film" in files["wizard.js"].text and "Renderuj" in files["review.js"].text
    css = files["style.css"].text
    assert "prefers-color-scheme: dark" in css and ':root[data-theme="dark"]' in css and 'not([data-theme="light"])' in css
    assert not any("@import" in r.text or "//fonts." in r.text or "cdn" in r.text.lower() for r in files.values())


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


# --- the redesigned UI's endpoints ---------------------------------------------------------
def test_quality_choice_becomes_the_cli_source(env):
    post = env.client.post
    good = env.submit(quality="good", profile="flight")
    assert good["options"]["source"] == "ffmpeg" and "quality" not in good["options"] and good["quality"] == "good"
    assert all(c.endswith("--source=ffmpeg") for c in good["commands"])
    fast = env.submit(quality="fast")
    assert (fast["options"]["source"], fast["quality"]) == ("proxy", "fast") and "--source=proxy" in fast["commands"][0]
    best = {"folder": str(env.lot1), "options": {"quality": "best"}}
    assert post("/api/jobs", json=best).status_code == 422                    # the stitcher is not built
    assert post("/api/jobs", json={"folder": str(env.lot1), "options": {"source": "sdk"}}).status_code == 422
    assert env.client.get("/api/meta").json()["stitcher"] is False
    env.stitcher = Path("insta-stitcher.exe")
    assert env.client.get("/api/meta").json()["stitcher"] is True
    assert env.submit(quality="best")["options"]["source"] == "sdk"
    plain = env.submit()                                                     # no choice: the CLI decides
    assert (plain["options"]["source"], plain["quality"]) == ("auto", "auto") and "--source" not in plain["commands"][0]
    assert post("/api/jobs", json={"folder": str(env.lot1), "options": {"quality": "ultra"}}).status_code == 422
    fake = FakeCli()
    env.submit(quality="good", profile="flight")
    env.runner(fake, python="py").run_once()
    assert fake.last[-1] == "--source=ffmpeg"


def test_shots_only_job_skips_the_film(env):
    only = env.submit(films=False)
    assert only["steps"] == ["analyze", "plan", "shots"] and only["options"]["shots"] is True
    reviewed = env.submit(films=False, review=True)
    assert reviewed["steps"] == ["analyze", "plan", "thumbnails"]
    fill_output(env.lot1 / "insta-roller")
    env.finish()
    assert env.client.post("/api/jobs/1/render").json()["steps"] == ["plan", "shots"]


def test_folder_summary_for_the_wizard(env, monkeypatch):
    client = env.client
    for name in ("VID_1.insv", "VID_2.insv", "LRV_1.lrv", "notes.txt"):
        (env.lot1 / name).write_text("x")
    monkeypatch.setattr(web, "probe_recordings", lambda folder: (2, 3725.04))
    found = client.get("/api/folders/inspect", params={"path": str(env.lot1)}).json()
    assert found == {"path": str(env.lot1), "recordings": 2, "duration_s": 3725.0, "full_quality": 2, "previews": 1,
                     "films": 0, "igc": 1, "kind": "flight", "usable": True, "out": str(env.lot1 / "insta-roller")}

    monkeypatch.setattr(web, "probe_recordings", lambda folder: (0, 0.0))
    empty = client.get("/api/folders/inspect", params={"path": str(env.lot2)}).json()
    assert (empty["kind"], empty["usable"], empty["igc"], empty["recordings"]) == ("holiday", False, 0, 0)

    def broken(folder):
        raise RuntimeError("FFmpeg not found")

    monkeypatch.setattr(web, "probe_recordings", broken)                     # lengths unknown: the files still count
    unknown = client.get("/api/folders/inspect", params={"path": str(env.lot1)}).json()
    assert (unknown["recordings"], unknown["duration_s"], unknown["usable"]) == (None, None, True)
    assert client.get("/api/folders/inspect", params={"path": str(env.lot2)}).json()["usable"] is False
    assert client.get("/api/folders/inspect", params={"path": "lot1"}).status_code == 400
    assert client.get("/api/folders/inspect", params={"path": str(env.root / "none")}).status_code == 404

    assert client.get("/api/folders").json()["recent"] == []
    env.submit()
    assert client.get("/api/folders").json()["recent"] == [{"name": "lot1", "path": str(env.lot1)}]


def test_timeline_is_a_barogram_for_a_flight(env):
    client = env.client
    job = env.submit(profile="flight")
    url = f"/api/jobs/{job['id']}/timeline"
    assert client.get(url).status_code == 409                               # no scenes yet
    out = fill_output(env.lot1 / "insta-roller")
    samples = [{"t": float(t), "alt_m": 200.0 + t % 97, "speed_kmh": 90.0, "vario_ms": 0.0, "bank_deg": 0.0} for t in range(900)]
    samples[431]["alt_m"] = 1987.0                                          # one short peak must survive the thinning
    (out / "telemetry.json").write_text(json.dumps({"version": 1, "source": "igc", "samples": samples}))
    data = client.get(url).json()
    assert data["kind"] == "flight" and data["duration_s"] == 900.0
    assert 100 < len(data["points"]) <= web.POINTS and [431.0, 1987.0] in data["points"]
    assert [p[0] for p in data["points"]] == sorted(p[0] for p in data["points"])
    assert data["points"][0][0] == 0.0 and data["points"][-1][0] >= 890.0
    assert sorted((s["reel"], s["type"], s["start_s"], s["duration_s"]) for s in data["shots"]) == [
        ("flight_summary_45s", "takeoff", 100.0, 5.0), ("thermal_30s", "thermal", 346.0, 8.0)]
    (out / "telemetry.json").unlink()                                       # scenes without telemetry: an empty chart
    assert client.get(url).json()["points"] == []
    assert client.get("/api/jobs/99/timeline").status_code == 404


def test_timeline_is_a_day_strip_for_a_holiday(env):
    job = env.submit(env.lot2, profile="holiday")
    out = env.lot2 / "insta-roller"
    out.mkdir()
    recs = [{"id": f"r{i}", "kind": "insv", "files": [f"{i}.insv"], "duration_s": 20.0 + i, "day": day}
            for i, day in enumerate(["20250812", "20250812", "20250814"])]
    scenes = [{"id": f"s{i}", "type": "view", "start_s": 2.0, "end_s": 9.0, "score": 0.5, "recording": r["id"],
               "attrs": {"day": r["day"]}} for i, r in enumerate(recs)]
    (out / "scenes.json").write_text(json.dumps({"version": 1, "source": "holiday", "recordings": recs, "scenes": scenes}))
    (out / "reels.json").write_text(json.dumps({"reels": [{"theme": "day_20250812", "tag": "day_20250812_30s", "status": "planned"}]}))
    shots = [{"index": 1, "scene_id": "s0", "type": "view", "start_s": 3.0, "duration_s": 4.0, "framing": ["view"]}]
    (out / "reel_plan_day_20250812_30s.json").write_text(json.dumps({"theme": "day_20250812", "shots": shots}))
    data = env.client.get(f"/api/jobs/{job['id']}/timeline").json()
    assert data == {"kind": "holiday", "shots": 1, "days": [
        {"day": "20250812", "label": "12.08.2025", "clips": 2, "duration_s": 41.0, "shots": 1},
        {"day": "20250814", "label": "14.08.2025", "clips": 1, "duration_s": 22.0, "shots": 0}]}
    assert env.job(job["id"])["kind"] == "holiday"


def test_films_have_titles_and_versions(env):
    client = env.client
    job = env.submit(shots=True)
    out = fill_output(env.lot1 / "insta-roller")
    for name in ("reel_thermal_30s.mp4", "reel_thermal_15s.mp4", "reel_day_20250812_60s.mp4", "reel_trip_180s.mp4"):
        (out / name).write_bytes(b"film")
    (out / "shots_thermal_30s").mkdir()
    (out / "shots_thermal_30s" / "01.mp4").write_bytes(b"shot")
    films = {f["name"]: f for f in client.get("/api/films").json()}
    assert len(films) == 4 and all((f["job"], f["job_name"]) == (job["id"], "lot1") for f in films.values())
    now = films["reel_thermal_30s.mp4"]
    assert (now["title"], now["theme"], now["length_s"], now["current"]) == ("Komin", "thermal", 30, True)
    assert films["reel_thermal_15s.mp4"]["current"] is False                 # an earlier version, another length
    assert films["reel_day_20250812_60s.mp4"]["title"] == "Dzień 12.08.2025" and films["reel_trip_180s.mp4"]["title"] == "Cały wyjazd"
    results = client.get(f"/api/jobs/{job['id']}/results").json()
    assert {f["name"]: f["title"] for f in results["films"]} == {name: f["title"] for name, f in films.items()}
    assert (results["shots"][0]["title"], results["shots"][0]["length_s"]) == ("Komin", 30)


def test_job_state_in_words_needs_phase_and_shot(env):
    job = env.submit(profile="flight")
    assert (job["name"], job["kind"], job["phase"], job["detail"]) == ("lot1", "flight", 0, None)
    env.queue.claim()
    env.queue.report(job["id"], 1, 0.4, ["$ py -m core.cli analyze x", "  shot 5/9", "$ py -m core.cli plan x", "plan: ok"])
    seen = env.job()
    assert (seen["stage"], seen["phase"], seen["detail"]) == ("plan", 1, None)      # the old step's shots do not count
    env.queue.report(job["id"], 2, 0.8, ["$ py -m core.cli render x", "  shot 2/9  scene", "  shot 3/9  scene"])
    seen = env.job()
    assert (seen["stage"], seen["phase"], seen["detail"]) == ("render", 2, {"shot": 3, "of": 9})
    assert seen["elapsed_s"] >= 0 and env.client.get("/api/jobs").json()[0]["detail"] == {"shot": 3, "of": 9}
    env.queue.finish(job["id"], "done")
    assert (env.job()["phase"], env.job()["detail"]) == (3, None)
    assert env.submit(env.lot2)["kind"] == "holiday"                         # no flight log in the folder


def test_status_tells_what_the_pc_has(env, monkeypatch):
    monkeypatch.setattr(web, "gpu_name", lambda: "Test GPU")
    status = env.client.get("/api/status").json()
    assert status["stitcher"] is False and status["gpu"] == "Test GPU"
    assert status["people"] == {"present": False, "count": 0} and status["cache"]["path"] == "" and status["cache"]["free_bytes"] > 0
    env.stitcher = Path("insta-stitcher.exe")
    env.people.write_text(json.dumps({"people": [{"name": "Ala", "embeddings": [[0.1], [0.2]]}, {"name": "Olek", "embeddings": []}]}))
    env.client.put("/api/settings", json={"work": str(env.tmp / "cache")})
    status = env.client.get("/api/status").json()
    assert status["stitcher"] is True and status["people"] == {"present": True, "count": 2}
    assert status["cache"]["path"] == str(env.tmp / "cache") and status["cache"]["free_bytes"] > 0


SECRET = "key-0123456789-secret"


def test_people_page_lists_people_and_never_shows_the_key(env):
    client = env.client
    page = client.get("/api/people").json()
    assert (page["present"], page["people"], page["refresh"]["status"]) == (False, [], "idle")
    assert page["env"] == {"IMMICH_TAILSCALE_ADDRESS": False, "IMMICH_ADDRESS": False, "IMMICH_API_KEY": False}
    holiday = web.yaml.safe_load((env.profiles / "holiday.yaml").read_text("utf-8"))
    assert page["names"] == list(holiday.get("people") or {})                # the default: the holiday profile's people

    env.people.write_text(json.dumps({"format": "insta-roller-people", "version": 1, "exported_at": "2026-11-23T15:15:24+00:00",
                                      "people": [{"name": "Ala", "embeddings": [[0.1]] * 50}, {"name": "Olek", "embeddings": [[0.2]] * 7}]}))
    env.env_file.write_text(f'# Immich\nIMMICH_ADDRESS="nas.example:2283"\nexport IMMICH_API_KEY={SECRET}\n', encoding="utf-8")
    seen = client.get("/api/people")
    page = seen.json()
    assert page["people"] == [{"name": "Ala", "samples": 50}, {"name": "Olek", "samples": 7}]
    assert page["present"] and page["exported_at"] == "2026-11-23T15:15:24+00:00"
    assert page["env"] == {"IMMICH_TAILSCALE_ADDRESS": False, "IMMICH_ADDRESS": True, "IMMICH_API_KEY": True}
    assert SECRET not in seen.text and "nas.example" not in seen.text


def test_people_refresh_runs_faces_export_with_the_key_in_the_environment(env, monkeypatch):
    client = env.client

    def refresh(names):
        return client.post("/api/people/refresh", json={"names": names})

    assert refresh(["Ala"]).status_code == 409                               # no key
    env.env_file.write_text(f"IMMICH_API_KEY={SECRET}\n", encoding="utf-8")
    assert refresh(["Ala"]).status_code == 409                               # no address
    env.env_file.write_text(f"IMMICH_ADDRESS=nas.example:2283\nIMMICH_API_KEY={SECRET}\n", encoding="utf-8")
    assert refresh([]).status_code == 422 and refresh(["Ala,Olek"]).status_code == 422 and refresh(["--out"]).status_code == 422
    assert env.immich.calls == []

    assert refresh(["Ala", " Łukasz Kowalski "]).json() == {"status": "running"}
    assert wait_for(lambda: client.get("/api/people").json()["refresh"]["status"] != "running")
    cmd, child_env = env.immich.calls[0]
    assert cmd[1:5] == ["-u", "-m", "core.cli", "faces-export"]              # the ordinary CLI command
    assert cmd[cmd.index("--people") + 1] == "Ala,Łukasz Kowalski" and cmd[cmd.index("--out") + 1] == str(env.people)
    assert cmd[cmd.index("--immich-url") + 1] == "http://nas.example:2283" and cmd[cmd.index("--api-key-env") + 1] == "IMMICH_API_KEY"
    assert child_env == {"IMMICH_API_KEY": SECRET} and SECRET not in " ".join(cmd)       # the key travels in the environment only
    seen = client.get("/api/people")
    page = seen.json()
    assert page["refresh"]["status"] == "done" and page["refresh"]["error"] == ""
    assert SECRET not in seen.text and "nas.example" not in seen.text        # not in the log either
    assert "%IMMICH_API_KEY%" in page["refresh"]["log"][1] and "%IMMICH_ADDRESS%" in page["refresh"]["log"][0]
    assert page["names"] == ["Ala", "Łukasz Kowalski"]                       # remembered for the next time
    assert json.loads((env.data / "settings.json").read_text("utf-8"))["people_names"] == ["Ala", "Łukasz Kowalski"]

    env.immich.code = 1                                                      # Immich refused
    monkeypatch.setenv("IMMICH_API_KEY", "from-the-environment")             # the environment wins over .env
    assert refresh(["Ala"]).status_code == 200
    assert wait_for(lambda: client.get("/api/people").json()["refresh"]["status"] != "running")
    page = client.get("/api/people").json()
    assert page["refresh"]["status"] == "error" and "401" in page["refresh"]["error"]
    assert env.immich.calls[1][1] == {"IMMICH_API_KEY": "from-the-environment"}


def test_people_refresh_tries_the_tailscale_address_first(env):
    import socket
    with socket.socket() as up:
        up.bind(("127.0.0.1", 0))
        up.listen()
        with socket.socket() as probe:                                       # a port nothing listens on
            probe.bind(("127.0.0.1", 0))
            down = probe.getsockname()[1]
        lines = f"IMMICH_ADDRESS=http://home.example:2283\nIMMICH_API_KEY={SECRET}\nIMMICH_TAILSCALE_ADDRESS="
        for tailscale, expected in ((f"127.0.0.1:{up.getsockname()[1]}", f"http://127.0.0.1:{up.getsockname()[1]}"),
                                    (f"127.0.0.1:{down}", "http://home.example:2283")):
            env.env_file.write_text(lines + tailscale + "\n", encoding="utf-8")
            assert env.client.post("/api/people/refresh", json={"names": ["Ala"]}).status_code == 200
            assert wait_for(lambda: env.client.get("/api/people").json()["refresh"]["status"] != "running")
            cmd = env.immich.calls[-1][0]
            assert cmd[cmd.index("--immich-url") + 1] == expected


def test_a_step_gets_extra_environment_and_utf8():
    lines: list[str] = []
    code = run_subprocess([sys.executable, "-c", "import os; print(os.environ['IMMICH_API_KEY'], os.environ['PYTHONIOENCODING'], 'Łukasz')"],
                          lines.append, lambda: False, {"IMMICH_API_KEY": "k"})
    assert (code, lines) == (0, ["k utf-8 Łukasz"])


def test_a_project_can_be_removed_from_the_list_but_its_files_stay(env):
    job = env.submit()
    out = fill_output(env.lot1 / "insta-roller")
    (out / "reel_thermal_30s.mp4").write_bytes(b"film")
    url = f"/api/jobs/{job['id']}"
    assert env.client.delete(url).status_code == 409                          # queued: stop it first
    env.finish()
    assert env.client.delete(url).json() == {"ok": True}
    assert env.client.get("/api/jobs").json() == [] and env.client.delete(url).status_code == 404
    assert (out / "reel_thermal_30s.mp4").exists() and (env.lot1 / "lot.igc").exists()
    assert env.submit()["existing"] is False                                  # the folder can become a project again


# --- the application window (app/desktop.py), without a display ----------------------------------------
from app import desktop                         # noqa: E402
from app.__main__ import parse, show_running    # noqa: E402
from app.steps import python_exe                # noqa: E402


class FakeWindow:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def create_file_dialog(self, kind, directory="", **more):
        self.asked.append((kind, directory))
        return self.answer


def test_folder_dialog_result_becomes_a_path(tmp_path):
    webview = pytest.importorskip("webview")
    window = FakeWindow((str(tmp_path / "lot"),))                             # pywebview answers with a tuple
    api = desktop.Api(window)
    assert api.choose_folder(str(tmp_path)) == str(tmp_path / "lot")
    kind, start = window.asked[0]
    assert start == str(tmp_path) and kind in (getattr(getattr(webview, "FileDialog", None), "FOLDER", None), webview.FOLDER_DIALOG)
    api.choose_folder(str(tmp_path / "gone"))                                 # a start folder that is not there is not passed on
    assert window.asked[1][1] == ""
    assert desktop.Api(FakeWindow(None)).choose_folder("") == "" and desktop.Api(FakeWindow(())).choose_folder("") == ""
    assert desktop.Api(FakeWindow(str(tmp_path))).choose_folder("") == str(tmp_path)
    assert not hasattr(desktop.Api, "window")                                 # nothing but the two functions is offered to the page
    assert [n for n in dir(desktop.Api) if not n.startswith("_")] == ["choose_folder", "reveal"]


def test_reveal_in_explorer_builds_the_command(tmp_path):
    film = tmp_path / "wyniki z lotu" / "reel_thermal_30s.mp4"
    film.parent.mkdir()
    film.write_bytes(b"film")
    started = []
    api = desktop.Api(None, launch=started.append)
    assert api.reveal(str(film)) is True and api.reveal(str(film.parent)) is True
    assert started == [["explorer.exe", "/select,", str(film)], ["explorer.exe", str(film.parent)]]
    assert api.reveal(str(tmp_path / "gone.mp4")) is False and api.reveal("reel.mp4") is False and len(started) == 2


def test_closing_the_window_quits_when_idle_and_asks_when_a_film_is_made():
    def never():
        raise AssertionError("nothing is running: no question")

    assert desktop.close_decision(False, never) == "quit"
    assert desktop.close_decision(True, lambda: "hide") == "hide"            # keep working, in the tray
    assert desktop.close_decision(True, lambda: "quit") == "quit"            # stop and close
    assert desktop.close_decision(True, lambda: "stay") == "stay" and desktop.close_decision(True, lambda: None) == "stay"


def test_start_options_choose_window_browser_or_nothing():
    assert parse([]).mode == "window" and parse(["--browser"]).mode == "browser" and parse(["--no-browser"]).mode == "headless"
    args = parse(["--no-browser", "--port", "9000", "--data", "x", "--host", "0.0.0.0"])
    assert (args.port, args.data, args.host) == (9000, "x", "0.0.0.0")
    assert parse(["--install-shortcut"]).install_shortcut and parse(["--uninstall-shortcut"]).uninstall_shortcut
    with pytest.raises(SystemExit):
        parse(["--browser", "--no-browser"])


def test_second_start_shows_the_running_app(monkeypatch):
    opened, posted = [], []
    monkeypatch.setattr("webbrowser.open", opened.append)
    assert show_running("http://127.0.0.1:1/", "window", posted.append) == "window"
    assert posted == ["http://127.0.0.1:1/api/window/show"] and opened == []

    def no_window(url):
        raise OSError("404")

    assert show_running("http://127.0.0.1:1/", "window", no_window) == "browser" and opened == ["http://127.0.0.1:1/"]
    assert show_running("http://127.0.0.1:1/", "browser") == "browser" and len(opened) == 2
    assert show_running("http://127.0.0.1:1/", "headless") == "nothing" and show_running("", "window") == "nothing"


def test_window_show_endpoint_calls_the_window(env):
    assert env.client.post("/api/window/show").status_code == 404            # --browser / --no-browser: no window
    shown = []
    env.app.state.show_window = lambda: shown.append(1)
    assert env.client.post("/api/window/show").json() == {"ok": True} and shown == [1]


def test_window_size_and_place_are_remembered(tmp_path):
    screens = [(0, 0, 1920, 1080)]
    assert desktop.load_geometry(tmp_path, screens) == {"width": 1320, "height": 900, "x": None, "y": None}
    desktop.save_geometry(tmp_path, 1000, 700, 120, 80)
    assert desktop.load_geometry(tmp_path, screens) == {"width": 1000, "height": 700, "x": 120, "y": 80}
    desktop.save_geometry(tmp_path, 160, 28, -32000, -32000)                  # minimised: not remembered
    assert desktop.load_geometry(tmp_path, screens)["width"] == 1000
    desktop.save_geometry(tmp_path, 1000, 700, 4000, 80)                      # a second monitor that is gone
    assert desktop.load_geometry(tmp_path, screens) == {"width": 1000, "height": 700, "x": None, "y": None}
    (tmp_path / "window.json").write_text("{broken")
    assert desktop.load_geometry(tmp_path, screens)["width"] == 1320


def test_tray_line_says_what_the_app_does(env):
    assert web.activity_text(env.queue.list()) == "Bezczynna"
    job = env.submit(profile="flight")
    assert web.activity_text(env.queue.list()) == "Zaraz zaczynam…"
    env.queue.claim()
    assert web.activity_text(env.queue.list()) == "lot1: Szukam scen…"
    env.queue.report(job["id"], 2, 0.7, ["$ py -m core.cli render x", "  shot 3/9  scene"])
    assert web.activity_text(env.queue.list()) == "lot1: Renderuję ujęcie 3 z 9"
    env.queue.finish(job["id"], "done")
    assert web.activity_text(env.queue.list()) == "Bezczynna"


def test_steps_run_with_the_console_python_and_films_carry_their_path(env, monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))     # the windowed app
    assert python_exe() == str(tmp_path / "python.exe") and Runner(env.queue).python == str(tmp_path / "python.exe")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert python_exe() == str(tmp_path / "python.exe")
    job = env.submit()
    out = fill_output(env.lot1 / "insta-roller")
    (out / "reel_thermal_30s.mp4").write_bytes(b"film")
    (out / "shots_thermal_30s").mkdir()
    (out / "shots_thermal_30s" / "01.mp4").write_bytes(b"shot")
    assert env.client.get("/api/films").json()[0]["path"] == str(out / "reel_thermal_30s.mp4")
    results = env.client.get(f"/api/jobs/{job['id']}/results").json()
    assert results["films"][0]["path"] == str(out / "reel_thermal_30s.mp4") and results["shots"][0]["path"] == str(out / "shots_thermal_30s")


def test_mark_is_drawn_as_an_icon(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    image = desktop.mark_image(64)
    assert image.size == (64, 64) and image.getpixel((32, 4))[:3] == desktop.ACCENT and image.getpixel((0, 0))[3] == 0
    assert any(image.getpixel((x, 22))[:3] == (255, 255, 255) for x in range(64))      # the wings are there
    icon = desktop.write_icon(tmp_path / "a" / "insta-roller.ico")
    with Image.open(icon) as stored:
        assert stored.format == "ICO" and (256, 256) in stored.info["sizes"] and (16, 16) in stored.info["sizes"]


@pytest.mark.skipif(os.name != "nt", reason="Start menu shortcuts are a Windows thing")
def test_shortcut_is_created_in_a_given_folder_and_removed(tmp_path):
    pytest.importorskip("PIL")
    menu, data = tmp_path / "Start Menu z odstępem", tmp_path / "data"
    link = desktop.install_shortcut(data, menu, arguments='-m app --data "D:\\moje dane"')
    assert link == menu / "insta-roller.lnk" and link.exists() and (data / "insta-roller.ico").exists()
    seen = desktop.read_shortcut(link)
    assert Path(seen["path"]) == Path(desktop.pythonw_exe()) and seen["arguments"] == '-m app --data "D:\\moje dane"'
    assert Path(seen["folder"]) == REPO and Path(seen["icon"]) == data / "insta-roller.ico"
    assert desktop.uninstall_shortcut(menu) is True and not link.exists() and desktop.uninstall_shortcut(menu) is False


def test_the_page_uses_the_window_when_it_is_in_one(env):
    get = lambda name: env.client.get(f"/static/{name}").text                # noqa: E731
    assert "pywebview" in get("ui.js") and "choose_folder" in get("ui.js") and "api.reveal" in get("ui.js")
    assert "Wybierz folder…" in get("wizard.js") and "chooseFolder" in get("settings.js")
    assert "Pokaż w Eksploratorze" in get("ui.js") and "saveButton" in get("films.js")
    assert env.client.get("/?okno=1").status_code == 200


def test_window_needs_webview2_and_says_why_not(monkeypatch):
    pytest.importorskip("webview")
    if os.name == "nt":
        monkeypatch.setattr(desktop, "webview2_version", lambda: "")
        assert "WebView2" in desktop.unavailable()
        monkeypatch.setattr(desktop, "webview2_version", lambda: "157.0.1.2")
        assert desktop.unavailable() == ""
    names = desktop.button_names()                                           # the close question names the real buttons
    assert len(names) == 3 and all(names) and not any("&" in n for n in names)


@pytest.mark.skipif(os.name != "nt", reason="Start menu shortcuts are a Windows thing")
def test_install_shortcut_command_writes_into_the_start_menu(monkeypatch, tmp_path, capsys):
    from app.__main__ import main
    monkeypatch.setattr(desktop, "start_menu", lambda: tmp_path / "menu")    # never the real Start menu in a test
    main(["--install-shortcut", "--data", str(tmp_path / "data")])
    link = tmp_path / "menu" / "insta-roller.lnk"
    assert link.exists() and "Shortcut created" in capsys.readouterr().out
    assert desktop.read_shortcut(link)["arguments"] == f'-m app --data "{tmp_path / "data"}"'
    main(["--uninstall-shortcut", "--data", str(tmp_path / "data")])
    assert not link.exists() and not (tmp_path / "data" / "app.lock").exists()   # no app was started for this

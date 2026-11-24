"""Stages 2, 3 and 5: themes and several reels, camera data and sync, holiday profile."""
from __future__ import annotations

import json
import math
import os
import struct
import time
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from core import audio, holiday, overlay, stitch, sync, telemetry
from core.config import SafeZone, Theme, load_mount, load_profile, load_theme
from core.contracts import ReelPlan, Scene, Scenes, Shot, Sync, Telemetry, TelemetrySample, RecordingInfo
from core.framing import frame_shot, plan_framing, safe_shift
from core.igc import parse_igc
from core.ingest import Recording
from core.phases import build_track, detect_phases, phases_from_gyro
from core.reel import plan_reels
from core.scenes import build_scenes, telemetry_samples

from . import synth


@pytest.fixture(scope="module")
def profile():
    return load_profile("flight")


@pytest.fixture(scope="module")
def mount(profile):
    return load_mount(profile.mount)


@pytest.fixture()
def scenes(tmp_path, profile):
    synth.write_igc(tmp_path / "f.igc")
    fixes = parse_igc(tmp_path / "f.igc")
    return build_scenes(detect_phases(fixes, profile.detection), fixes[0].t + 30, 3600.0, profile)[0]


def _themes(profile, **enabled):
    themes = [load_theme(n) for n in profile.themes]
    for t in themes:
        t.enabled = enabled.get(t.name, t.enabled)
    return themes


# --- stage 2 ---------------------------------------------------------------
def test_surprise_reel_varies_its_framing_and_speeds_up_the_launch(scenes, profile, mount):
    plans, _ = plan_reels(scenes, profile, _themes(profile), mount)
    reel = next(p for p in plans if p.theme == "surprise")
    thermals = [tuple(s.framing) for s in reel.shots if s.type == "thermal"]
    assert len(thermals) > 1 and len(set(thermals)) == len(thermals)    # each thermal looks different
    assert all(s.speed == 6 for s in reel.shots if s.type == "tow")
    assert abs(reel.length_s - sum(s.duration_s / s.speed for s in reel.shots)) < 0.05
    framed = plan_framing(reel, profile, mount)
    assert {s.projection for s in framed.shots} >= {"planet", "fisheye"}


def test_zoom_and_roll_happen_while_a_preset_is_held(mount):
    zoom = frame_shot(["pilot_close"], 6.0, mount, 1.5)
    assert zoom[0].fov > zoom[-1].fov and zoom[0].yaw == zoom[-1].yaw      # a push in, the camera does not turn
    roll = frame_shot(["barrel"], 6.0, mount, 1.5)
    assert roll[-1].roll - roll[0].roll == 360                             # once over, ends upright
    reveal = frame_shot(["planet", "planet_front"], 8.0, mount, 1.5)
    assert reveal[0].pitch == -90 and reveal[-1].pitch == 0 and reveal[0].fov > reveal[-1].fov


def test_holiday_surprise_is_one_more_film_of_the_whole_trip():
    profile = load_profile("holiday")
    mount = load_mount(profile.mount)
    scenes = [Scene(id=f"{d}_{i}", type=t, start_s=5, end_s=45, score=0.6, recording=f"{d}_{i}", attrs={"day": d})
              for d in ("20250101", "20250102") for i, t in enumerate(["view", "view", "panorama", "walk"])]
    job = SimpleNamespace(profile=profile, mount=mount, themes=[load_theme(n) for n in profile.themes],
                          length=None, args=SimpleNamespace(mode="day"))
    plans, entries = holiday.plan(job, SimpleNamespace(scenes=scenes))
    extra = next(p for p in plans if p.theme == "holiday_surprise")
    assert extra.title == "Zaskocz mnie" and {s.scene_id[:8] for s in extra.shots} == {"20250101", "20250102"}
    assert all(s.speed == 8 for s in extra.shots if s.type == "walk")
    assert ["planet", "planet_view"] in [s.framing for s in extra.shots]
    assert sum(p.theme.startswith("day_") for p in plans) == 2


def test_a_shot_cannot_mix_projections(profile, mount):
    reel = ReelPlan(theme="x", target_length_s=6, length_s=6, shots=[
        Shot(index=1, scene_id="a", type="thermal", start_s=0, duration_s=6, framing=["front", "planet"])])
    with pytest.raises(ValueError, match="projections"):
        plan_framing(reel, profile, mount)


def test_several_reels_share_no_scene_and_landing_is_off(scenes, profile, mount):
    plans, entries = plan_reels(scenes, profile, _themes(profile), mount)
    by_theme = {p.theme: p for p in plans}
    assert {"takeoff_tow", "thermal", "glide_views", "flight_summary"} <= set(by_theme)
    borrowers = {t.name for t in _themes(profile) if t.borrows}
    own = [s.scene_id for p in plans if p.theme not in borrowers for s in p.shots]
    assert len(own) == len(set(own))                                 # a scene is in one reel only
    assert not any(s.type in ("circuit", "landing") for p in plans for s in p.shots)
    assert by_theme["flight_summary"].shots[-1].type == "ending"
    landing = next(e for e in entries if e.theme == "landing")
    assert landing.status == "skipped" and "switched off" in landing.reason


def test_landing_theme_can_be_switched_on(scenes, profile, mount):
    plans, _ = plan_reels(scenes, profile, _themes(profile, landing=True), mount)
    by_theme = {p.theme: p for p in plans}
    assert {s.type for s in by_theme["landing"].shots} <= {"circuit", "landing"}
    summary = by_theme["flight_summary"]
    assert summary.shots[-1].type == "landing"                       # the recorded landing ends the summary


def test_too_little_material_gives_no_reel(scenes, profile, mount):
    themes = _themes(profile)
    next(t for t in themes if t.name == "thermal").min_material_s = 10_000
    plans, entries = plan_reels(scenes, profile, themes, mount)
    assert "thermal" not in {p.theme for p in plans}
    assert "not enough material" in next(e for e in entries if e.theme == "thermal").reason


def test_a_theme_is_just_a_yaml_file(scenes, profile, mount, tmp_path):
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "wings.yaml").write_text(yaml.safe_dump({
        "name": "wings", "include": ["thermal"], "framing": {"thermal": ["wing_outer"]}, "target_length_s": 12}))
    theme = load_theme("wings", tmp_path)
    plans, _ = plan_reels(scenes, profile, [theme], mount)
    assert plans[0].length_s <= 12.01
    assert {tuple(s.framing) for s in plans[0].shots} <= {("wing_left",), ("wing_right",)}


def test_safe_zone_moves_the_subject_into_the_free_area(mount):
    dyaw, dpitch = safe_shift(120, 1080, 1920, SafeZone(bottom=0.2, right=0.12))
    assert dyaw > 0 and dpitch < 0          # camera turns right and down: the subject moves left and up
    plain = frame_shot(["pilot"], 4.0, mount, 1.5)[0]
    shifted = frame_shot(["pilot"], 4.0, mount, 1.5, SafeZone(bottom=0.2, right=0.12))[0]
    assert shifted.yaw > plain.yaw and shifted.pitch < plain.pitch
    assert shifted.ev == mount.presets["pilot"].exposure_ev > 0


def test_locked_horizon_counters_the_bank(profile, mount):
    tele = Telemetry(source="t", samples=[TelemetrySample(t=t, speed_kmh=90, alt_m=1000, vario_ms=1, bank_deg=30)
                                          for t in range(0, 20)])
    reel = ReelPlan(theme="t", target_length_s=4, length_s=4, shots=[
        Shot(index=1, scene_id="s", type="glide", start_s=5, duration_s=4, framing=["front_level"]),
        Shot(index=2, scene_id="s", type="glide", start_s=5, duration_s=4, framing=["front"])])
    level, plain = plan_framing(reel, profile, mount, tele).shots
    assert len(level.keyframes) > 4 and all(abs(abs(k.roll) - 30) < 1 for k in level.keyframes)
    assert all(k.roll == 0 for k in plain.keyframes)


def test_overlay_frames_cover_the_shot(profile):
    tele = Telemetry(source="t", samples=[TelemetrySample(t=t, speed_kmh=80 + t, alt_m=900 + t, vario_ms=1.5,
                                                          bank_deg=0) for t in range(0, 10)])
    cfg = profile.overlay.model_copy(update={"enabled": True, "bottom": None})    # the default place: inside the safe zone
    w, h = overlay.canvas_size(cfg, 1080)
    data = overlay.frames_rgba(cfg, tele, 1080, 2.0, 1.0)
    assert len(data) == w * h * 4 * int(cfg.fps) and any(data[3::4][:20000])
    assert overlay.frames_rgba(cfg, tele, 1080, 500.0, 1.0) == b""          # no telemetry there
    x, y = overlay.position(cfg, (1080, 1920), profile.safe_zone)
    assert y + h <= 1920 * (1 - profile.safe_zone.bottom)


# --- stage 3 ---------------------------------------------------------------
def _camera_file(path, gps_rows, imu_rows, first_frame):
    """A file with a version-3 trailer as the camera writes it."""
    def varint(v):
        out = b""
        while True:
            out += bytes([v & 127 | (128 if v > 127 else 0)])
            v >>= 7
            if not v:
                return out
    meta = b"\x12\x0bInsta360 X5" + b"\xc0\x01" + varint(first_frame)       # fields 2 and 24
    gps = b"".join(struct.pack("<QH", int(t), int(t % 1 * 1000)) + b"A" + struct.pack("<d", lat) + b"N"
                   + struct.pack("<d", lon) + b"E" + struct.pack("<ddd", 20.0, 90.0, alt)
                   for t, lat, lon, alt in gps_rows)
    imu = b"".join(struct.pack("<Q6H", t, *[int(v + 32768) for v in vals]) for t, vals in imu_rows)
    body, index = b"", b""
    for rid, data in ((0x101, meta), (0x3, imu), (0x7, gps)):
        index += struct.pack("<HII", rid, len(data), len(body))
        body += data
    tail = struct.pack("<HI", 0, len(index)) + bytes(32)
    size = len(body) + len(index) + len(tail) + 8 + 32
    path.write_bytes(b"VIDEO" * 10 + body + index + tail + struct.pack("<II", size, 3) + telemetry.MAGIC)


def test_gps_and_imu_are_read_from_the_camera_file(tmp_path):
    imu = [(1_000_000 + i * 1000, (-1024, 0, 0, 0, 164, 0)) for i in range(4000)]      # 4 s at 1 kHz
    gps = [(1782208018.0 + i / 10, 53.1 + i * 1e-5, 23.2, 150.0 + i) for i in range(40)]
    _camera_file(tmp_path / "VID.insv", gps, imu, first_frame=1_500_000)
    cam = telemetry.read_file(tmp_path / "VID.insv")
    assert cam.model == "Insta360 X5" and cam.has_gps
    assert cam.imu[0, 0] == pytest.approx(-0.49, abs=0.02)             # the IMU starts before the first frame
    assert cam.imu[:, 1].mean() == pytest.approx(-1.0) and cam.imu[:, 5].mean() == pytest.approx(10.0, abs=0.1)
    assert cam.gps[0, 1] == pytest.approx(53.1) and cam.gps[-1, 3] == pytest.approx(189.0)
    assert cam.gps_start_utc == pytest.approx(1782208018.5, abs=0.01)  # GPS record 0 sits at the first IMU sample
    assert telemetry.read_file(tmp_path / "missing_trailer.bin" if (tmp_path / "missing_trailer.bin").write_bytes(
        b"x" * 200) else None).has_gps is False


def test_sync_matches_gyro_turns_to_the_track(tmp_path, profile):
    synth.write_igc(tmp_path / "f.igc")
    fixes = parse_igc(tmp_path / "f.igc")
    t = np.array([f.t for f in fixes])
    lat, lon = np.array([f.lat for f in fixes]), np.array([f.lon for f in fixes])
    rate, _ = sync.track_turn_rate(t, lat, lon)
    true_start = fixes[0].t + 100.0
    vt = np.arange(0, 1000, 0.02)
    gyro_up = -np.interp(true_start + vt, t, rate)                    # camera upright: up = -x, turn = -gyro.up
    imu = np.column_stack([vt, -np.ones_like(vt), np.zeros_like(vt), np.zeros_like(vt),
                           gyro_up * -1.0, np.zeros_like(vt), np.zeros_like(vt)])
    cam = telemetry.CameraData("X5", np.zeros((0, 6)), imu)
    result = sync.synchronise(cam, 1000.0, true_start - 14.0, (t, lat, lon), False, 90.0, 0.4)   # clock 14 s off
    assert result.method.startswith("gyro") and result.correlation > 0.8
    assert result.video_start_utc == pytest.approx(true_start, abs=0.5)
    no_track = sync.synchronise(cam, 1000.0, true_start - 14.0, None, False, 90.0, 0.4)
    assert no_track.method == "camera clock"


def test_flight_without_any_position_still_has_scenes(profile):
    turn = [0.0] * 200 + [-18.0] * 240 + [0.0] * 200 + [18.0] * 120 + [0.0] * 100
    phases = phases_from_gyro(turn, 1000.0, profile.detection)
    thermals = [p for p in phases if p.type == "thermal"]
    assert [p.attrs["direction"] for p in thermals] == ["left", "right"]
    assert thermals[0].start == pytest.approx(1200, abs=8) and any(p.type == "glide" for p in phases)
    scenes, _ = build_scenes(phases, 1000.0, 860.0, profile)
    assert {s.type for s in scenes} == {"thermal", "glide"}


def test_motor_glider_engine_off_from_sound(tmp_path, profile):
    level = np.concatenate([np.full(400, -12.0), np.full(600, -30.0)]) + np.random.default_rng(1).normal(0, 1, 1000)
    second = audio.engine_off(level, 8.0, 20)
    assert second == pytest.approx(400, abs=3)
    assert audio.engine_off(np.full(500, -12.0), 8.0, 20) is None
    synth.write_igc(tmp_path / "f.igc")
    fixes = parse_igc(tmp_path / "f.igc")
    phases = detect_phases(fixes, profile.detection, "motorglider", fixes[0].t + 300)
    types = [p.type for p in phases]
    assert "climb" in types and "engine_off" in types and "tow" not in types and "release" not in types
    off = next(p for p in phases if p.type == "engine_off")
    assert (off.start + off.end) / 2 == pytest.approx(fixes[0].t + 300, abs=1)
    motor = load_profile("flight_motor")
    assert {"climb", "engine_off"} <= set(motor.scene_types) and load_mount(motor.mount).aliases["pilot_wide"] == "crew"


def test_telemetry_follows_the_track(tmp_path, profile):
    synth.write_igc(tmp_path / "f.igc")
    fixes = parse_igc(tmp_path / "f.igc")
    samples = telemetry_samples(build_track(fixes, profile.detection), fixes[0].t + 30, 3600.0)
    at = {round(s.t): s for s in samples}
    assert at[500].bank_deg < -20 and at[900].bank_deg > 20           # left thermal, then right
    assert at[200].speed_kmh == pytest.approx(108, abs=3) and at[200].vario_ms == pytest.approx(2.5, abs=0.2)


# --- stage 4 (cache) ---------------------------------------------------------
def test_cache_drops_the_least_recently_used(tmp_path):
    for i, name in enumerate(["old", "mid", "new"]):
        (tmp_path / name).write_bytes(b"x" * 1000)
        os.utime(tmp_path / name, (time.time() - 100 + i * 10,) * 2)
    removed = stitch.prune(tmp_path, 2500)
    assert [p.name for p in removed] == ["old"] and (tmp_path / "new").exists()
    assert [p.name for p in stitch.prune(tmp_path, 500, keep={tmp_path / "new"})] == ["mid"]


# --- stage 5 ---------------------------------------------------------------
def _stick_imu(spans):
    """spans: (seconds, up vector, gyro deg/s) -> IMU rows at 50 Hz."""
    rows, t = [], 0.0
    for seconds, up, rate in spans:
        for _ in range(int(seconds * 50)):
            rows.append([t, *up, rate, 0.0, 0.0])
            t += 0.02
    return np.array(rows)


def test_holiday_days_and_lowered_camera():
    holiday_profile = load_profile("holiday")
    cfg = holiday_profile.holiday
    assert holiday.day_of("20250802_235900", cfg) == "20250802"
    assert holiday.day_of("20250803_013000", cfg) == "20250802"       # after midnight: still the evening before
    assert holiday.day_of("20250803_093000", cfg) == "20250803"

    upright, lowered = (-0.95, -0.3, 0.1), (0.2, -0.3, -0.93)
    imu = _stick_imu([(12, upright, 40.0), (10, lowered, 60.0), (16, upright, 2.0), (30, upright, 40.0)])
    rec = Recording("20250802_120000", "insv", [], [68.0], None, (3840, 3840))
    base = {k: v.base_score for k, v in holiday_profile.scene_types.items()}
    scenes = holiday.clip_scenes(rec, telemetry.CameraData("X5", np.zeros((0, 6)), imu), np.zeros(0), cfg, base,
                                 "20250802")
    kinds = [s.type for s in scenes]
    assert kinds == ["view", "panorama", "walk"]                       # the lowered stretch is gone
    assert all(not (s.start_s < 21 and s.end_s > 13) for s in scenes)
    assert scenes[0].start_s >= cfg.trim_s and scenes[-1].end_s <= 68.0 - cfg.trim_s
    assert scenes[0].attrs["level"][0] == pytest.approx(math.degrees(math.atan2(0.3, 0.95)), abs=2)      # stick leans towards the holder


def test_holiday_day_films_and_trip_film_from_one_analysis():
    profile = load_profile("holiday")
    mount = load_mount(profile.mount)
    scenes = []
    for day, n in (("20250802", 6), ("20250803", 2)):
        for i in range(n):
            rec = f"{day}_1{i}0000"
            scenes.append(Scene(id=f"{rec}_00_view", type="view", start_s=2, end_s=14, score=0.6, recording=rec,
                                attrs={"day": day, "level": [0, 0], "yaw_holder": 180.0, "yaw_travel": 0.0}))
    info = RecordingInfo(id="x", kind="insv", files=[], duration_s=1)
    data = Scenes(fingerprint="f", recording=info, sync=Sync(video_start_utc=0, offset_s=0, method="m"),
                  missing_phases=[], scenes=scenes)

    def job(mode, length=None):
        return SimpleNamespace(profile=profile, mount=mount, themes=[load_theme("holiday_day")], length=length,
                               args=SimpleNamespace(mode=mode))
    plans, entries = holiday.plan(job("both"), data)
    names = [p.theme for p in plans]
    assert names == ["day_20250802", "day_20250803", "trip"]
    trip = plans[-1]
    days = [s.recording[:8] for s in trip.shots]
    assert days == sorted(days) and set(days) == {"20250802", "20250803"}
    per_day = {d: sum(s.duration_s for s in trip.shots if s.recording[:8] == d) for d in set(days)}
    assert per_day["20250802"] > per_day["20250803"] >= min(profile.holiday.min_per_day_s, 16) - 0.1
    assert holiday.levels(None, data)[scenes[0].id] == (0, 0)
    framing = plan_framing(plans[0], profile, mount)
    assert (framing.width, framing.height) == (1920, 1080) and framing.shots[0].keyframes[0].roll == 0


def test_camera_never_turns_faster_than_the_limit(mount):
    quick = frame_shot(["front", "pilot"], 10.0, mount, 1.5)
    slow = frame_shot(["front", "pilot"], 10.0, mount, 1.5, max_turn_dps=45)
    assert quick[2].t - quick[1].t == pytest.approx(1.5)
    assert slow[2].t - slow[1].t == pytest.approx(4.0)                 # half a turn at 45 degrees a second
    assert abs(slow[2].yaw - slow[1].yaw) / (slow[2].t - slow[1].t) <= 45.01


def test_companion_shot_follows_the_face_slowly_and_at_its_height():
    stick = load_mount("selfie_stick")
    preset = stick.presets["companion"]
    attrs = {"yaw_person": 100.0, "pitch_person": -20.0,
             "track_person": [[10.0, 60.0, -18.0], [14.0, 100.0, -20.0], [18.0, 170.0, -24.0]]}
    frames = frame_shot(["companion"], 8.0, stick, 2.0, attrs=attrs, start_s=10.0, max_track_dps=10)
    assert frames[0].yaw == pytest.approx(60.0) and frames[0].pitch == pytest.approx(-18.0 + preset.pitch)
    for a, b in zip(frames, frames[1:]):
        assert abs(b.yaw - a.yaw) <= 10 * (b.t - a.t) + 0.01            # it lags rather than whips round
    assert frames[-1].yaw < 170 and frames[-1].pitch == pytest.approx(-24.0 + preset.pitch)
    assert load_theme("holiday_day").max_per_type["holder"] == 2


# --- AI framing ---------------------------------------------------------------
def _ai_reel(profile, mount, scenes):
    theme = load_theme("thermal")
    plans, _ = plan_reels(scenes, profile, [theme], mount)
    return theme, plans[0]


def test_ai_candidates_are_ordinary_views_of_the_same_scene_type(scenes, profile, mount):
    from core import aiframe
    theme, reel = _ai_reel(profile, mount, scenes)
    by_id = {s.id: s for s in scenes}
    themes = [load_theme(n) for n in profile.themes]
    shot = reel.shots[0]
    options = aiframe.candidates(shot, by_id[shot.scene_id], profile, theme, mount, themes)
    assert options[0] == shot.framing and len(options) > 3
    assert all(mount.presets[n].projection == "flat" for names in options for n in names)   # no planet outside "surprise"


def test_ai_local_choice_prefers_the_best_score_but_not_the_same_view_every_time():
    from core import aiframe
    options = [["a"], ["b"], ["a", "b"]]
    score = {"a": 0.9, "b": 0.5}
    assert aiframe.pick(options, score, {}, None, 0.1) == ["a"]
    assert aiframe.pick(options, score, {("a",): 1}, ("a",), 0.15) == ["a", "b"]
    good, bad = np.eye(4)[:1], np.eye(4)[1:2]
    scores = aiframe.clip_scores(np.eye(4)[[0, 1]], good, bad)
    assert scores[0] > 0.99 and scores[1] < 0.01


def test_ai_openrouter_aims_the_camera_itself_and_bad_answers_are_ignored(scenes, profile, mount, tmp_path, monkeypatch):
    from core import aiframe
    theme, reel = _ai_reel(profile, mount, scenes)
    profile = profile.model_copy(deep=True)
    profile.ai_framing.engine = "openrouter"
    profile.ai_framing.accents = 1
    profile.ai_framing.person_shots = 1
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    from PIL import Image

    def stills(shot, presets, ctx, folder):
        Image.new("RGB", (32, 32), "blue").save(folder / "x.jpg")
        return {name: folder / "x.jpg" for name in presets}
    monkeypatch.setattr(aiframe, "_stills", stills)
    monkeypatch.setattr(aiframe, "_pano", lambda shot, ctx, folder: stills(shot, ["p"], ctx, folder)["p"])
    monkeypatch.setattr(aiframe, "_own_stills", lambda shot, own, ctx, folder: stills(shot, list(own), ctx, folder))
    by_id = {s.id: s for s in scenes}
    themes = [load_theme(n) for n in profile.themes]
    names, extra = aiframe.views(reel.shots[0], by_id[reel.shots[0].scene_id], profile, theme, mount, themes)
    assert {"planet", "bubble"} <= extra and "pilot_close" in names      # showpieces of "surprise" are accents here
    letter = {n: chr(65 + i) for i, n in enumerate(names)}
    seen = []

    def post(body):
        seen.append(body)
        parts = body["messages"][0]["content"]
        if len(seen) == 2:                      # the review round: the model sees its own view and aims it again
            assert "REVIEW" in parts[-3]["text"] and "ai1a" in parts[-2]["text"] and parts[-1]["type"] == "image_url"
            return json.dumps({"shots": [
                {"shot": 1, "seen": "airfield below the left wing", "why": "ground, then up along the wing",
                 "views": [{"yaw": -95, "pitch": -70, "fov": 500, "zoom_to": 120, "pan": 9000, "horizon": "level"}, letter["wing_left"]]},
                {"shot": 2, "views": [letter["pilot_close"]], "why": "the pilot"}]})
        assert "360" in parts[0]["text"] and "PANORAMA" in parts[0]["text"] and "[accent]" in parts[1]["text"]
        assert sum(part["type"] == "image_url" for part in parts) == 2 * len(reel.shots)     # panorama + ready views
        return json.dumps({"shots": [
            {"shot": 1, "seen": "airfield below the left wing", "why": "ground, then up along the wing",
             "views": [{"yaw": -60, "pitch": -20, "fov": 100}, letter["wing_left"]]},
            {"shot": 2, "views": [letter["pilot_close"]], "why": "the pilot"},
            {"shot": 9, "views": ["A"]}, {"shot": "x"}]})
    ctx = SimpleNamespace(work=tmp_path)
    out = aiframe.apply(reel, scenes, profile, mount, theme, ctx, post=post)
    first = out.shots[0]
    assert first.framing == ["ai1a", "wing_left"] and "airfield" in first.attrs["ai"]
    own = first.attrs["views"]["ai1a"]
    assert own["yaw"] == -95 and own["pitch"] == -70 and own["fov"] == 140 and own["fov_end"] == 120    # clamped to a sane lens
    assert abs(own["pan_dps"]) <= profile.max_turn_dps and own["horizon"] == "locked"
    assert out.shots[1].framing == ["pilot_close"]
    framed = plan_framing(out, profile, mount)
    assert framed.shots[0].keyframes[0].pitch < -50                  # the model's own view is what gets framed
    # Showpieces and shots of the pilot are rationed; a path may not change projection.
    item = {"shot": reel.shots[0], "names": names, "extra": extra, "stills": {}, "pano": None}
    batch = [{**item, "shot": reel.shots[0].model_copy(update={"index": i})} for i in (1, 2, 3, 4, 5)]
    reply = json.dumps({"shots": [
        {"shot": 1, "views": [{"yaw": 0, "pitch": -90, "fov": 300, "projection": "planet"}]},
        {"shot": 2, "views": [letter["bubble"]]},
        {"shot": 3, "views": [letter["planet"], letter["front_in"]]},
        {"shot": 4, "views": [{"yaw": 180, "pitch": 0, "fov": 90}]},
        {"shot": 5, "views": [letter["wing_left"], {"yaw": 175, "pitch": 5, "fov": 100}]}]})
    found = aiframe.parse_reply(reply, batch, profile, mount, False, 1, 1)
    assert sorted(found) == [1, 4, 5] and found[5][0] == ["wing_left"]      # the second pilot view is dropped from its path
    aiframe.apply(reel, scenes, profile, mount, theme, ctx, post=post)
    assert len(seen) == 2                                           # first answer + review; cached after that


def test_ai_views_are_described_for_the_model(mount):
    from core import aiframe
    assert "zooms in" in aiframe.describe("pilot_close", mount, "flight") and "pilot" in aiframe.describe("pilot_close", mount, "flight")
    assert "small-planet" in aiframe.describe("planet", mount, "flight") and "straight down" in aiframe.describe("planet", mount, "flight")
    assert "rolls" in aiframe.describe("barrel", mount, "flight")
    holiday_mount = load_mount("selfie_stick")
    assert "person holding the camera" in aiframe.describe("holder", holiday_mount, "holiday")
    short = Shot(index=1, scene_id="a", type="glide", start_s=0, duration_s=4, framing=["front"])
    assert aiframe.valid_sequence(["front"], short, mount.presets)
    assert not aiframe.valid_sequence(["front", "pilot"], short, mount.presets)     # 2 s per view is too little
    assert aiframe.shows_person(mount.presets["pilot_wide"], mount) and not aiframe.shows_person(mount.presets["wing_left"], mount)


def test_ai_engine_that_cannot_run_keeps_the_theme_framing(scenes, profile, mount, tmp_path, monkeypatch):
    from core import aiframe
    theme, reel = _ai_reel(profile, mount, scenes)
    profile = profile.model_copy(deep=True)
    profile.ai_framing.engine = "openrouter"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(aiframe, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(aiframe, "_stills", lambda shot, presets, ctx, folder: {})
    monkeypatch.setattr(aiframe, "_pano", lambda shot, ctx, folder: None)
    said = []
    out = aiframe.apply(reel, scenes, profile, mount, theme, SimpleNamespace(work=tmp_path), progress=said.append)
    assert out == reel and "OPENROUTER_API_KEY" in said[0]


def test_overlay_can_sit_lower_than_the_safe_zone():
    from core.config import Overlay
    zone = SafeZone(top=0.08, bottom=0.20, left=0.0, right=0.12)
    frame = (1080, 1920)
    high = overlay.position(Overlay(enabled=True), frame, zone)
    low = overlay.position(Overlay(enabled=True, bottom=0.07), frame, zone)
    assert low[0] == high[0] and low[1] > high[1] + 200
    assert low[1] + overlay.canvas_size(Overlay(enabled=True), 1080)[1] <= 1920 * 0.93 + 1

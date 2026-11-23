"""Stages 2, 3 and 5: themes and several reels, camera data and sync, holiday profile."""
from __future__ import annotations

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
def test_several_reels_share_no_scene_and_landing_is_off(scenes, profile, mount):
    plans, entries = plan_reels(scenes, profile, _themes(profile), mount)
    by_theme = {p.theme: p for p in plans}
    assert {"takeoff_tow", "thermal", "glide_views", "flight_summary"} <= set(by_theme)
    own = [s.scene_id for p in plans if p.theme != "flight_summary" for s in p.shots]
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
    cfg = profile.overlay.model_copy(update={"enabled": True})
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

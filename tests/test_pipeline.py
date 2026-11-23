from __future__ import annotations

import subprocess

import pytest

from core import contracts, ffmpeg
from core.cli import main
from core.config import load_mount, load_profile, load_theme
from core.contracts import FramingPlan, ReelPlan, Scenes
from core.framing import frame_shot, plan_framing
from core.igc import parse_igc
from core.phases import detect_phases
from core.reel import plan_reel
from core.render import sendcmd_script
from core.scenes import build_scenes

from . import synth

TOLERANCE_S = 6


@pytest.fixture(scope="module")
def profile():
    return load_profile("flight")


@pytest.fixture(scope="module")
def mount(profile):
    return load_mount(profile.mount)


@pytest.fixture(scope="module")
def theme(profile):
    return load_theme("flight_summary")


@pytest.fixture()
def flight(tmp_path, profile):
    truth = synth.write_igc(tmp_path / "flight.igc")
    fixes = parse_igc(tmp_path / "flight.igc")
    return fixes, detect_phases(fixes, profile.detection), truth


def _rel(phases, fixes, kind):
    return [(p.start - fixes[0].t, p.end - fixes[0].t, p.attrs) for p in phases if p.type == kind]


def test_igc_parse(tmp_path):
    synth.write_igc(tmp_path / "f.igc")
    fixes = parse_igc(tmp_path / "f.igc")
    assert fixes[0].t == synth.START.timestamp()
    assert fixes[1].t - fixes[0].t == 1
    assert fixes[0].lat == pytest.approx(52.0, abs=1e-4)
    assert fixes[0].lon == pytest.approx(21.0, abs=1e-4)
    assert fixes[0].alt == synth.GROUND_ALT


def test_phases_match_the_known_flight(flight):
    fixes, phases, truth = flight
    assert [p.type for p in phases] == [
        "takeoff", "tow", "release", "glide", "thermal", "glide", "thermal", "glide", "circuit", "landing"]

    takeoff = _rel(phases, fixes, "takeoff")[0]
    assert takeoff[0] == pytest.approx(truth["takeoff"], abs=TOLERANCE_S)

    release = _rel(phases, fixes, "release")[0]
    assert (release[0] + release[1]) / 2 == pytest.approx(truth["release"], abs=TOLERANCE_S)

    left, right = _rel(phases, fixes, "thermal")
    assert left[2]["direction"] == "left" and right[2]["direction"] == "right"
    assert left[0] == pytest.approx(truth["thermal_left"][0], abs=TOLERANCE_S)
    assert left[1] == pytest.approx(truth["thermal_left"][1], abs=TOLERANCE_S)
    assert right[0] == pytest.approx(truth["thermal_right"][0], abs=TOLERANCE_S)
    assert right[1] == pytest.approx(truth["thermal_right"][1], abs=TOLERANCE_S)
    assert left[2]["climb_ms"] == pytest.approx(2.0, abs=0.2)

    landing = _rel(phases, fixes, "landing")[0]
    assert landing[0] < truth["touchdown"] < landing[1]


def test_log_that_ends_in_the_air_has_no_landing(tmp_path, profile):
    synth.write_igc(tmp_path / "f.igc", landing=False)
    phases = detect_phases(parse_igc(tmp_path / "f.igc"), profile.detection)
    types = {p.type for p in phases}
    assert "landing" not in types and "circuit" not in types
    assert "thermal" in types


def _plan(flight, profile, theme, mount, length, video_offset=30.0, duration=3600.0):
    fixes, phases, _ = flight
    scenes, missing = build_scenes(phases, fixes[0].t + video_offset, duration, profile)
    return scenes, missing, plan_reel(scenes, profile, theme, mount, length, barred=frozenset({"circuit", "landing"}))


def test_reel_respects_length_theme_and_order(flight, profile, theme, mount):
    _, _, reel = _plan(flight, profile, theme, mount, 45)
    assert 40 <= reel.length_s <= 45.01
    assert not {s.type for s in reel.shots} & {"circuit", "landing"}
    starts = [s.start_s for s in reel.shots]
    assert starts == sorted(starts)
    assert reel.shots[-1].type == "ending" and reel.shots[-1].framing == ["pilot_wide"]
    for a, b in zip(reel.shots, reel.shots[1:]):
        assert a.start_s + a.duration_s <= b.start_s + 0.01

    _, _, short = _plan(flight, profile, theme, mount, 20)
    assert short.length_s <= 20.01 and len(short.shots) < len(reel.shots)


def test_inner_wing_follows_the_circling_direction(flight, profile, theme, mount):
    scenes, _, reel = _plan(flight, profile, theme, mount, 60)
    by_id = {s.id: s for s in scenes}
    thermals = [s for s in reel.shots if s.type == "thermal"]
    assert len(thermals) == 2
    for shot in thermals:
        side = by_id[shot.scene_id].attrs["direction"]
        assert shot.framing == [f"wing_{side}", f"back_{side}"]     # wing and ground, no pilot shot


def test_low_thermal_scores_higher_and_is_cut_from_its_start(flight, profile, theme, mount):
    scenes, _, reel = _plan(flight, profile, theme, mount, 60)
    low, high = [s for s in scenes if s.type == "thermal"]
    assert low.attrs["agl_m"] == pytest.approx(690, abs=30) and low.attrs.get("low")
    assert high.attrs["agl_m"] > profile.thermal_low_agl_m and "low" not in high.attrs
    plain = profile.scene_types["thermal"].base_score + 0.6 * low.attrs["climb_ms"] / profile.thermal_good_climb_ms
    assert low.score > plain
    shot = next(s for s in reel.shots if s.scene_id == low.id)
    assert shot.start_s == pytest.approx(low.start_s, abs=0.01)


def test_recording_that_ends_early_reports_missing_phases(flight, profile, theme, mount):
    scenes, missing, reel = _plan(flight, profile, theme, mount, 30, duration=600.0)
    assert "landing" in missing and "circuit" in missing
    assert all(s.end_s <= 600 for s in scenes)
    assert reel.shots


def test_framing_takes_the_short_way_round(mount):
    frames = frame_shot(["back_left", "pilot_wide"], 8.0, mount, 1.5)
    assert [f.t for f in frames] == sorted(f.t for f in frames)
    assert frames[0].t == 0 and frames[-1].t == 8.0
    start = mount.presets["back_left"].yaw
    assert frames[0].yaw == start and frames[-1].yaw == -180  # the short way, not through the nose
    assert abs(frames[-1].yaw - start) < 180
    script = sendcmd_script(frames)
    assert "[expr] v360@flat yaw" in script and "TI*TI*(3-2*TI)" in script


def test_plans_round_trip_through_json(flight, profile, theme, mount, tmp_path):
    _, _, reel = _plan(flight, profile, theme, mount, 45)
    framing = plan_framing(reel, profile, mount)
    contracts.write(tmp_path / "reel.json", reel)
    contracts.write(tmp_path / "framing.json", framing)
    assert contracts.read(tmp_path / "reel.json", ReelPlan) == reel
    assert contracts.read(tmp_path / "framing.json", FramingPlan) == framing
    assert (framing.width, framing.height) == (1080, 1920)


def _ffmpeg_available() -> bool:
    try:
        ffmpeg.ffmpeg_exe()
        return True
    except RuntimeError:
        return False


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_one_command_from_folder_to_reel(tmp_path, capsys):
    job = tmp_path / "job"
    job.mkdir()
    synth.write_igc(job / "flight.igc")
    # A 20-minute equirectangular stand-in at a tiny size.
    subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=s=320x160:r=5:d=1200", "-c:v", "libx264", "-preset", "ultrafast",
                    "-g", "5", str(job / "export.mp4")], check=True)
    common = [str(job), "--video-start", "2026-06-24T10:00:30Z", "--themes", "takeoff_tow,flight_summary"]

    main(["run", *common, "--length", "20", "--shots"])
    out = job / "insta-roller"
    reel = out / "reel_flight_summary_20s.mp4"
    assert reel.exists()
    plan = contracts.read(out / "framing_plan_flight_summary_20s.json", FramingPlan)
    assert ffmpeg.probe(reel).duration_s == pytest.approx(plan.length_s, abs=0.5)
    assert ffmpeg.probe(reel).video_sizes[0] == (1080, 1920)
    scenes = contracts.read(out / "scenes.json", Scenes)
    assert "landing" in scenes.missing_phases          # the stand-in stops after 20 minutes

    # A new length makes a new version without analysing again.
    analysed = (out / "scenes.json").stat().st_mtime_ns
    capsys.readouterr()
    main(["run", *common, "--length", "12"])
    assert "analyze: cached" in capsys.readouterr().out
    assert (out / "scenes.json").stat().st_mtime_ns == analysed
    assert (out / "reel_flight_summary_12s.mp4").exists() and reel.exists()

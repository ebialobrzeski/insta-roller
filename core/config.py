"""Profile, mount and theme configuration loaded from profiles/*.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Output(Strict):
    width: int
    height: int
    fps: float


class Detection(Strict):
    takeoff_speed_ms: float
    landing_speed_ms: float
    speed_hold_s: int
    takeoff_scene_s: float
    release_vario_ms: float
    release_window_s: int
    release_min_after_takeoff_s: int
    release_min_gain_m: float
    release_scene_s: float
    circling_rate_dps: float
    circling_gap_s: int
    thermal_min_s: int
    thermal_min_turn_deg: float
    split_long_s: float = 0.0           # glides, climbs and thermals longer than twice this become several scenes; 0 = never
    glide_min_s: int
    circuit_agl_m: float
    circuit_max_s: int
    final_s: float
    rollout_s: float
    turn_smooth_s: int
    vario_smooth_s: int
    engine_off_drop_db: float = 8.0     # motor glider: sustained drop of the sound level
    engine_off_hold_s: int = 20


class SceneType(Strict):
    weight: float
    base_score: float
    min_shot_s: float
    max_shot_s: float
    anchor: Literal["start", "center", "end"]
    framing: list[str]
    speed: float = 1.0                  # playback speed; above 1 the shot is sped up and muted


class SafeZone(Strict):
    """Parts of the frame covered by the TikTok / Instagram interface, as fractions."""
    top: float = 0.0
    bottom: float = 0.0
    left: float = 0.0
    right: float = 0.0


class Overlay(Strict):
    enabled: bool = False
    speed: bool = True
    altitude: bool = True
    vario: bool = True
    speed_max_kmh: float = 200.0
    scale: float = 1.0
    fps: float = 10.0
    # Free space under the overlay, as a fraction of the frame height.
    # None = sit on top of the safe zone's bottom edge (clear of the app's captions).
    bottom: float | None = None


class SyncConfig(Strict):
    search_s: float = 90.0              # how far the camera clock may be off
    min_correlation: float = 0.4


class Holiday(Strict):
    """Settings of the walking / selfie-stick analysis (profile `holiday`)."""
    day_starts_at_h: float = 4.0        # the day boundary, local time
    utc_offset_h: float | None = None   # None = the computer's time zone
    trim_s: float = 3.0                 # cut from both ends of every clip
    lowered_tilt_deg: float = 55.0      # stick further than this from upright = camera lowered
    window_s: float = 4.0
    still_dps: float = 6.0              # below this rotation rate the holder stands still
    still_min_s: float = 6.0
    walk_min_s: float = 8.0
    min_per_day_s: float = 20.0
    trip_length_s: float = 180.0
    long_walk_s: float = 20.0           # walking longer than this is shown sped up
    speech_db: float = 6.0              # sound this far above the clip's floor counts as speech


class AiFraming(Strict):
    """A model picks the framing of every shot from the candidates (core/aiframe.py)."""
    engine: Literal["off", "local", "openrouter"] = "off"
    model: str = "anthropic/claude-sonnet-5.5"      # OpenRouter model, must accept images
    # openrouter: tokens the model may spend thinking before it answers (0 = no extended reasoning).
    # More gives a more considered film and costs more; models without reasoning ignore it.
    reasoning_tokens: int = 0
    variety: float = 0.08               # local: what a framing loses each time the reel already used it
    brief: str = ""                     # openrouter: one more sentence of instructions
    accents: int = 2                    # openrouter: unusual shots (planet, fisheye, roll) allowed in an ordinary reel
    person_shots: int = 2               # openrouter: shots of a reel that may show the pilot / the holder
    # local: sentences a good / a poor still is compared with
    good: list[str] = ["a beautiful scenic photo with a clear subject and a visible horizon",
                       "a well composed photo of a person, whole face in the frame"]
    bad: list[str] = ["a blurry photo of glare and reflections", "a photo of nothing, empty sky",
                      "a badly framed photo with the subject cut off at the edge"]


class Profile(Strict):
    name: str
    kind: Literal["flight", "holiday", "custom"] = "flight"
    aircraft: Literal["glider", "motorglider"] = "glider"
    output: Output
    target_length_s: float
    audio: bool
    audio_fade_s: float = 0.25
    mount: str
    themes: list[str]
    encoder: str
    transition_s: float
    max_turn_dps: float = 60.0          # the camera never turns faster than this between presets
    max_track_dps: float = 15.0         # ... nor while it follows a person
    shot_handles_s: float = 1.5
    safe_zone: SafeZone = SafeZone()
    overlay: Overlay = Overlay()
    sync: SyncConfig = SyncConfig()
    holiday: Holiday = Holiday()
    people: dict[str, float] = {}       # person name -> screen-time weight
    ai_framing: AiFraming = AiFraming()
    person_bonus: float = 0.2           # score added to a scene with a known person
    thermal_good_climb_ms: float = 3.0
    thermal_low_agl_m: float = 800.0
    thermal_low_bonus: float = 0.0
    detection: Detection | None = None
    scene_types: dict[str, SceneType]


class Preset(Strict):
    yaw: float
    pitch: float
    roll: float = 0.0
    fov: float
    horizon: Literal["aircraft", "locked"] = "aircraft"
    exposure_ev: float = 0.0
    safe_zone: bool = True              # False: frame the preset on the full picture
    pan_dps: float = 0.0                # slow turn while the preset is held (panorama)
    fov_end: float | None = None        # zoom while the preset is held: fov goes from `fov` to this
    roll_turns: float = 0.0             # whole rolls while the preset is held (1 = a barrel roll)
    track: str | None = None            # follow a direction signal: travel | holder | person
    # flat = an ordinary lens; planet = the whole surroundings wrapped into a ball
    # (stereographic); fisheye = a very wide round view without stretched edges.
    projection: Literal["flat", "planet", "fisheye"] = "flat"


class Calibration(Strict):
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    lens_fov: float | None = None
    width: int | None = None            # equirect width of the rough FFmpeg stitch (default: native)
    rorder: str = "ypr"                 # order in which v360 applies the three angles
    stabilized: bool = False            # True: the source already has a level horizon
    stabilize: str = "none"             # Media SDK stitch: none | flowstate | direction-lock
    auto_level: bool = False            # level every clip with the camera's gravity sensor
    # Flight: the camera is not in a fixed place. Level and nose direction of every scene
    # are worked out from its sensors (core/mountcal.py) instead of yaw / pitch / roll above.
    auto_nose: bool = False


class Mount(Strict):
    name: str
    calibration: dict[str, Calibration]
    aliases: dict[str, str] = {}        # preset name used by profiles and themes -> preset of this mount
    presets: dict[str, Preset]


class Ending(Strict):
    preset: str
    duration_s: float


class Theme(Strict):
    name: str
    title: str = ""
    enabled: bool = True
    borrows: bool = False               # may reuse scenes that other reels already took
    gates: list[str] = []               # scene types nobody may use while this theme is disabled
    include: list[str]
    max_per_type: dict[str, int] = {}
    framing: dict[str, list[str]] = {}  # per scene type, replaces the profile's framing
    # Several framings per scene type, taken in turn by the shots of that type,
    # so a reel does not show the same trick twice in a row. Wins over `framing`.
    variants: dict[str, list[list[str]]] = {}
    speed: dict[str, float] = {}        # per scene type, replaces the profile's playback speed
    target_length_s: float | None = None
    min_material_s: float = 0.0         # less usable footage than this: no reel
    # How many reels of this theme to make. Each further one takes other scenes,
    # so a long recording gives several different reels instead of one.
    count: int = 1
    ending: Ending | None = None


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile(name: str, root: Path = PROFILES_DIR) -> Profile:
    path = Path(name) if str(name).endswith((".yaml", ".yml")) else root / f"{name}.yaml"
    return Profile.model_validate(_load(path))


def load_mount(name: str, root: Path = PROFILES_DIR) -> Mount:
    return Mount.model_validate(_load(root / "mounts" / f"{name}.yaml"))


def load_theme(name: str, root: Path = PROFILES_DIR) -> Theme:
    return Theme.model_validate(_load(root / "themes" / f"{name}.yaml"))


def list_names(kind: str = "", root: Path = PROFILES_DIR) -> list[str]:
    """Names of the profiles ('') or of the 'mounts' / 'themes'."""
    return sorted(p.stem for p in (root / kind).glob("*.yaml"))

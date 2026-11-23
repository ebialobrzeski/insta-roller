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
    glide_min_s: int
    circuit_agl_m: float
    circuit_max_s: int
    final_s: float
    rollout_s: float
    turn_smooth_s: int
    vario_smooth_s: int


class SceneType(Strict):
    weight: float
    base_score: float
    min_shot_s: float
    max_shot_s: float
    anchor: Literal["start", "center", "end"]
    framing: list[str]


class Profile(Strict):
    name: str
    output: Output
    target_length_s: float
    audio: bool
    mount: str
    theme: str
    encoder: str
    transition_s: float
    thermal_good_climb_ms: float
    detection: Detection
    scene_types: dict[str, SceneType]


class Preset(Strict):
    yaw: float
    pitch: float
    roll: float = 0.0
    fov: float


class Calibration(Strict):
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    lens_fov: float | None = None


class Mount(Strict):
    name: str
    calibration: dict[str, Calibration]
    presets: dict[str, Preset]


class Ending(Strict):
    preset: str
    duration_s: float


class Theme(Strict):
    name: str
    include: list[str]
    max_per_type: dict[str, int] = {}
    ending: Ending | None = None


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_profile(name: str, root: Path = PROFILES_DIR) -> Profile:
    return Profile.model_validate(_load(root / f"{name}.yaml"))


def load_mount(name: str, root: Path = PROFILES_DIR) -> Mount:
    return Mount.model_validate(_load(root / "mounts" / f"{name}.yaml"))


def load_theme(name: str, root: Path = PROFILES_DIR) -> Theme:
    return Theme.model_validate(_load(root / "themes" / f"{name}.yaml"))

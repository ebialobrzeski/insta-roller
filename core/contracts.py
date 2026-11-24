"""Data contracts between pipeline steps. Steps talk only through these files.

Run `insta-roller schemas` to export the JSON schemas.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T", bound=BaseModel)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1


class Sync(BaseModel):
    video_start_utc: float   # unix time of video second 0
    offset_s: float          # manual correction that is already included above
    method: str
    correlation: float | None = None


class RecordingInfo(BaseModel):
    id: str
    kind: str                # equirect | dual_fisheye | insv
    files: list[str]
    duration_s: float
    start_utc: float | None = None
    day: str = ""            # holiday: the local day the clip belongs to


class Scene(BaseModel):
    id: str
    type: str
    start_s: float           # video time, clipped to the recording
    end_s: float
    score: float
    attrs: dict = {}
    recording: str = ""      # recording id; empty = the job's only recording


class Scenes(Contract):
    fingerprint: str
    recording: RecordingInfo         # the main (or first) recording
    recordings: list[RecordingInfo] = []
    sync: Sync
    source: str = "igc"              # where the scenes come from: igc | camera_gps | gyro | holiday
    missing_phases: list[str]        # flight phases that the recording does not cover
    scenes: list[Scene]


class TelemetrySample(BaseModel):
    t: float                 # video time, s
    speed_kmh: float
    alt_m: float
    vario_ms: float
    bank_deg: float          # positive = right wing down, from turn rate and speed
    heading_deg: float | None = None     # where the nose points, 0 = north (log + gyroscope)
    lat: float | None = None
    lon: float | None = None


class Telemetry(Contract):
    source: str
    samples: list[TelemetrySample]   # one per second
    home: list[float] | None = None  # [lat, lon, alt] of the place the flight started from


class Shot(BaseModel):
    index: int
    scene_id: str
    type: str
    start_s: float
    duration_s: float            # source seconds
    framing: list[str]           # resolved preset names, in order
    recording: str = ""
    speed: float = 1.0
    attrs: dict = {}
    second: list[str] = []       # split frame: the views of the lower picture


class ReelPlan(Contract):
    theme: str
    target_length_s: float
    length_s: float
    shots: list[Shot]
    edited: bool = False         # set by a manual edit; `plan` then leaves the file alone
    title: str = ""
    removed: bool = False        # taken out by hand: the plan is kept, the reel is not made
    take: int = 0                # raised to have the reel made anew: other scenes and framings are chosen


class Keyframe(BaseModel):
    t: float                     # seconds from shot start
    yaw: float                   # may run past +-180 so moves take the short way
    pitch: float
    roll: float
    fov: float                   # diagonal
    ev: float = 0.0              # exposure correction


class FramedShot(BaseModel):
    index: int
    scene_id: str
    start_s: float
    duration_s: float
    keyframes: list[Keyframe]    # smoothstep between consecutive keyframes
    recording: str = ""
    speed: float = 1.0
    type: str = ""
    projection: str = "flat"     # flat | planet | fisheye, the same for the whole shot
    second: list[Keyframe] = []  # split frame: the camera of the lower picture (always a normal lens)


class FramingPlan(Contract):
    theme: str
    length_s: float
    width: int
    height: int
    fps: float
    shots: list[FramedShot]
    source: str = ""             # fingerprint of the reel plan this was made from


class ReelEntry(BaseModel):
    theme: str
    tag: str
    status: str                  # planned | skipped
    reason: str = ""
    length_s: float = 0.0
    shots: int = 0


class ReelSet(Contract):
    """What `plan` decided: which reels exist and which were left out, and why."""
    reels: list[ReelEntry]


class JobState(Contract):
    steps: dict[str, dict] = {}
    outputs: list[str] = []


CONTRACTS = {
    "scenes": Scenes,
    "reel_plan": ReelPlan,
    "framing_plan": FramingPlan,
    "telemetry": Telemetry,
    "reels": ReelSet,
    "job_state": JobState,
}


def write(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def read(path: Path, cls: type[T]) -> T:
    return cls.model_validate_json(path.read_text(encoding="utf-8"))


def export_schemas(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, cls in CONTRACTS.items():
        (folder / f"{name}.schema.json").write_text(
            json.dumps(cls.model_json_schema(), indent=2), encoding="utf-8")

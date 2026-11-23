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


class RecordingInfo(BaseModel):
    id: str
    kind: str                # equirect | dual_fisheye
    files: list[str]
    duration_s: float


class Scene(BaseModel):
    id: str
    type: str
    start_s: float           # video time, clipped to the recording
    end_s: float
    score: float
    attrs: dict = {}


class Scenes(Contract):
    fingerprint: str
    recording: RecordingInfo
    sync: Sync
    missing_phases: list[str]    # flight phases that the recording does not cover
    scenes: list[Scene]


class Shot(BaseModel):
    index: int
    scene_id: str
    type: str
    start_s: float
    duration_s: float
    framing: list[str]           # resolved preset names, in order


class ReelPlan(Contract):
    theme: str
    target_length_s: float
    length_s: float
    shots: list[Shot]


class Keyframe(BaseModel):
    t: float                     # seconds from shot start
    yaw: float                   # may run past +-180 so moves take the short way
    pitch: float
    roll: float
    fov: float                   # diagonal


class FramedShot(BaseModel):
    index: int
    scene_id: str
    start_s: float
    duration_s: float
    keyframes: list[Keyframe]    # smoothstep between consecutive keyframes


class FramingPlan(Contract):
    theme: str
    length_s: float
    width: int
    height: int
    fps: float
    shots: list[FramedShot]


class JobState(Contract):
    steps: dict[str, dict] = {}
    outputs: list[str] = []


CONTRACTS = {
    "scenes": Scenes,
    "reel_plan": ReelPlan,
    "framing_plan": FramingPlan,
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

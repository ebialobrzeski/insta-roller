"""Sensor data stored by the camera at the end of every .insv / .lrv file.

The file ends with a trailer: a 32-byte magic, the trailer size, and an index
of records. Used here:

- 0x101 metadata (protobuf): camera model, first video frame timestamp,
- 0x3   IMU at about 1 kHz: timestamp + 3 accelerometer + 3 gyroscope values,
- 0x7   GPS from the GPS remote at 10 Hz, stamped with UTC.

The IMU and the video share the camera's microsecond clock. GPS records carry
UTC and are written from the first IMU sample on, ten per second, which ties
UTC to the video clock without any manual offset.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAGIC = b"8db42d694ccc418790edff439fe026bf"
REC_METADATA, REC_IMU, REC_GPS = 0x101, 0x3, 0x7
META_MODEL, META_FIRST_FRAME = 2, 24

ACCEL_LSB_PER_G = 1024.0
GYRO_LSB_PER_DPS = 16.4          # +-2000 deg/s over 16 bits
GPS_HZ = 10.0
IMU_OUT_HZ = 50

_GPS = np.dtype([("t", "<u8"), ("ms", "<u2"), ("fix", "S1"), ("lat", "<f8"), ("ns", "S1"),
                 ("lon", "<f8"), ("ew", "S1"), ("speed", "<f8"), ("track", "<f8"), ("alt", "<f8")])
_IMU = np.dtype([("t", "<u8"), ("v", "<u2", (6,))])


@dataclass
class CameraData:
    """Telemetry of one recording on the joined video timeline."""
    model: str
    gps: np.ndarray      # valid fixes only; rows: utc, lat, lon, alt, speed_ms, track_deg
    imu: np.ndarray      # rows: video_t, ax, ay, az [g], gx, gy, gz [deg/s], at IMU_OUT_HZ
    gps_start_utc: float | None = None   # UTC of video second 0 by the position of the GPS records

    @property
    def has_gps(self) -> bool:
        return len(self.gps) >= 20

    @property
    def has_imu(self) -> bool:
        return len(self.imu) > IMU_OUT_HZ * 5


def read_index(f) -> dict[int, tuple[int, int]]:
    """Record id -> (file offset, size). Empty when the file has no trailer."""
    f.seek(0, 2)
    size = f.tell()
    if size < 78:
        return {}
    f.seek(size - 78)
    tail = f.read(78)
    if tail[-32:] != MAGIC:
        return {}
    index_id, index_size = struct.unpack("<HI", tail[:6])
    trailer_size, version = struct.unpack("<II", tail[-40:-32])
    start = size - trailer_size
    out: dict[int, tuple[int, int]] = {}
    if version >= 3 and index_id == 0:
        f.seek(size - 78 - index_size)
        index = f.read(index_size)
        for i in range(0, index_size - 9, 10):
            rid, rsize, offset = struct.unpack("<HII", index[i:i + 10])
            if rsize:
                out[rid] = (start + offset, rsize)
        return out
    # Older layout: records are chained backwards, each followed by id and size.
    end = size - 72
    while end - 6 > start:
        f.seek(end - 6)
        rid, rsize = struct.unpack("<HI", f.read(6))
        if rsize <= 0 or end - 6 - rsize < start:
            break
        out[rid if rid < 0x100 or rid == REC_METADATA else rid >> 8] = (end - 6 - rsize, rsize)
        end -= 6 + rsize
    return out


def _protobuf(data: bytes) -> dict[int, object]:
    """Top-level fields only; the first value of each field number."""
    out: dict[int, object] = {}
    i = 0

    def varint() -> int:
        nonlocal i
        value = shift = 0
        while True:
            c = data[i]
            i += 1
            value |= (c & 127) << shift
            shift += 7
            if c < 128:
                return value

    try:
        while i < len(data):
            key = varint()
            field, wire = key >> 3, key & 7
            if wire == 0:
                value = varint()
            elif wire == 1:
                value = data[i:i + 8]
                i += 8
            elif wire == 2:
                n = varint()
                value = data[i:i + n]
                i += n
            elif wire == 5:
                value = data[i:i + 4]
                i += 4
            else:
                break
            out.setdefault(field, value)
    except IndexError:
        pass
    return out


def _records(f, index, rid: int, dtype: np.dtype) -> np.ndarray:
    if rid not in index:
        return np.zeros(0, dtype)
    offset, size = index[rid]
    f.seek(offset)
    return np.frombuffer(f.read(size - size % dtype.itemsize), dtype)


def read_file(path: Path, video_offset_s: float = 0.0) -> CameraData:
    """Telemetry of one camera file; video_offset_s places it on the joined timeline."""
    with open(path, "rb") as f:
        index = read_index(f)
        meta: dict[int, object] = {}
        if REC_METADATA in index:
            f.seek(index[REC_METADATA][0])
            meta = _protobuf(f.read(index[REC_METADATA][1]))
        raw_imu = _records(f, index, REC_IMU, _IMU)
        raw_gps = _records(f, index, REC_GPS, _GPS)

    model = meta.get(META_MODEL, b"")
    model = model.decode("utf-8", "replace") if isinstance(model, bytes) else ""
    imu = np.zeros((0, 7))
    gps = np.zeros((0, 6))
    start_utc = None
    t0 = video_offset_s
    if len(raw_imu):
        first_frame = meta.get(META_FIRST_FRAME)
        if not isinstance(first_frame, int):
            first_frame = int(raw_imu["t"][0])
        t = (raw_imu["t"].astype(np.int64) - first_frame) / 1e6 + video_offset_s
        values = raw_imu["v"].astype(np.float64) - 32768.0
        values[:, :3] /= ACCEL_LSB_PER_G
        values[:, 3:] /= GYRO_LSB_PER_DPS
        rate = (len(t) - 1) / max(t[-1] - t[0], 1e-6)
        block = max(int(round(rate / IMU_OUT_HZ)), 1)
        n = len(t) // block * block
        imu = np.column_stack([t[:n].reshape(-1, block).mean(1), values[:n].reshape(-1, block, 6).mean(1)])
        t0 = float(t[0])
    if len(raw_gps):
        lat = np.where(raw_gps["ns"] == b"S", -raw_gps["lat"], raw_gps["lat"])
        lon = np.where(raw_gps["ew"] == b"W", -raw_gps["lon"], raw_gps["lon"])
        utc = raw_gps["t"].astype(np.float64) + raw_gps["ms"] / 1000.0
        valid = (raw_gps["fix"] == b"A") & (raw_gps["t"] > 0) & ((lat != 0) | (lon != 0))
        gps = np.column_stack([utc, lat, lon, raw_gps["alt"], raw_gps["speed"], raw_gps["track"]])[valid]
        if valid[0]:        # records are written ten per second from the first IMU sample on
            start_utc = float(utc[0]) - t0
    return CameraData(model, gps, imu, start_utc)


def read_recording(files: list[Path], durations: list[float]) -> CameraData:
    """Joins the telemetry of a take that the camera split into several files."""
    parts, offset = [], 0.0
    for path, length in zip(files, durations):
        part = read_file(telemetry_file(path), offset)
        if len(part.imu):   # each file carries a little data past its own video
            part.imu = part.imu[(part.imu[:, 0] >= offset) & (part.imu[:, 0] < offset + length)]
        parts.append(part)
        offset += length
    if not parts:
        return CameraData("", np.zeros((0, 6)), np.zeros((0, 7)))
    gps = np.vstack([p.gps for p in parts])
    if len(gps):
        gps = gps[np.concatenate([[True], np.diff(gps[:, 0]) > 0])]
    return CameraData(parts[0].model, gps, np.vstack([p.imu for p in parts]), parts[0].gps_start_utc)


def telemetry_file(path: Path) -> Path:
    """Camera files carry their own trailer; an export may sit next to the originals."""
    return path


def turn_about_vertical(imu: np.ndarray) -> np.ndarray:
    """Turn rate about the aircraft's vertical axis, deg/s, positive = right turn.

    The vertical axis is the mean accelerometer direction over the recording.
    """
    up = imu[:, 1:4].mean(0)
    up /= np.linalg.norm(up) or 1.0
    return -(imu[:, 4:7] @ up)


def gravity_tilt(imu: np.ndarray) -> tuple[float, float]:
    """(pitch, roll) in degrees of the mean gravity direction in the camera frame."""
    ax, ay, az = imu[:, 1:4].mean(0)
    return (float(np.degrees(np.arctan2(ax, np.hypot(ay, az)))), float(np.degrees(np.arctan2(ay, az))))

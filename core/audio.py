"""Sound level of a recording, used for analysis only (engine off, speech)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from . import ffmpeg

RATE = 8000


def level_db(path: Path, start_s: float = 0.0, duration_s: float | None = None) -> np.ndarray:
    """RMS level in dB for every second of the file's first audio track (empty without audio)."""
    args = [ffmpeg.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{start_s:.3f}"]
    if duration_s:
        args += ["-t", f"{duration_s:.3f}"]
    args += ["-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    res = subprocess.run(args, capture_output=True, **ffmpeg.QUIET)
    if res.returncode != 0 or len(res.stdout) < RATE * 2:
        return np.zeros(0)
    pcm = np.frombuffer(res.stdout[:len(res.stdout) // (2 * RATE) * 2 * RATE], np.int16).astype(np.float64)
    rms = np.sqrt((pcm.reshape(-1, RATE) ** 2).mean(1))
    return 20 * np.log10(np.maximum(rms, 1.0) / 32768.0)


def engine_off(level: np.ndarray, drop_db: float, hold_s: int, after_s: int = 0) -> int | None:
    """Second at which the level falls by drop_db and stays down: the engine stops."""
    best, best_drop = None, drop_db
    for i in range(max(after_s, hold_s), len(level) - hold_s):
        drop = float(np.median(level[i - hold_s:i]) - np.median(level[i:i + hold_s]))
        if drop >= best_drop:
            best, best_drop = i, drop
    return best

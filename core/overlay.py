"""Flight data overlay: speed dial, altitude and vario, drawn with Pillow.

The frames are piped to FFmpeg as raw RGBA and laid over the shot, inside the
area the platform interface leaves free.
"""
from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .config import Overlay, SafeZone
from .contracts import Telemetry

BASE_W, BASE_H = 600, 250        # canvas at scale 1, for a 1080 px wide frame
SS = 2                           # supersampling for smooth edges
GREEN = (60, 220, 110, 255)
WHITE = (255, 255, 255, 255)
DIM = (255, 255, 255, 150)
SHADE = (0, 0, 0, 110)
SWEEP_FROM, SWEEP = 135.0, 270.0


@lru_cache
def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def canvas_size(cfg: Overlay, frame_width: int) -> tuple[int, int]:
    k = cfg.scale * frame_width / 1080.0
    return int(BASE_W * k) // 2 * 2, int(BASE_H * k) // 2 * 2


def position(cfg: Overlay, frame: tuple[int, int], zone: SafeZone) -> tuple[int, int]:
    """Bottom-left corner of the free area, or lower when the profile says so (overlay.bottom)."""
    w, h = canvas_size(cfg, frame[0])
    margin = int(frame[0] * 0.03)
    if cfg.bottom is not None:
        return int(frame[0] * zone.left) + margin, max(int(frame[1] * (1 - cfg.bottom)) - h, 0)
    return int(frame[0] * zone.left) + margin, int(frame[1] * (1 - zone.bottom)) - h - margin


class Sampler:
    """Telemetry values at any video time, linear between the 1 Hz samples."""

    def __init__(self, telemetry: Telemetry):
        self.samples = sorted(telemetry.samples, key=lambda s: s.t)

    def covers(self, start_s: float, end_s: float) -> bool:
        return bool(self.samples) and self.samples[0].t <= end_s and self.samples[-1].t >= start_s

    def at(self, t: float) -> tuple[float, float, float]:
        s = self.samples
        lo, hi = 0, len(s) - 1
        if t <= s[0].t:
            return s[0].speed_kmh, s[0].alt_m, s[0].vario_ms
        if t >= s[-1].t:
            return s[-1].speed_kmh, s[-1].alt_m, s[-1].vario_ms
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if s[mid].t <= t:
                lo = mid
            else:
                hi = mid
        a, b = s[lo], s[hi]
        k = (t - a.t) / max(b.t - a.t, 1e-6)
        mix = lambda x, y: x + (y - x) * k  # noqa: E731
        return mix(a.speed_kmh, b.speed_kmh), mix(a.alt_m, b.alt_m), mix(a.vario_ms, b.vario_ms)


def _dial(cfg: Overlay, size: tuple[int, int]) -> tuple[Image.Image, tuple[float, float, float]]:
    """The parts of the speed dial that never change, and its centre and radius."""
    w, h = size[0] * SS, size[1] * SS
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = h * 0.46
    cx, cy = r + h * 0.04, h / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SHADE)
    ring = r * 0.80
    d.arc([cx - ring, cy - ring, cx + ring, cy + ring], SWEEP_FROM, SWEEP_FROM + SWEEP, fill=DIM, width=int(r * 0.07))
    steps = 5
    for i in range(steps + 1):
        a = math.radians(SWEEP_FROM + SWEEP * i / steps)
        value = f"{cfg.speed_max_kmh * i / steps:.0f}"
        d.text((cx + math.cos(a) * r * 0.58, cy + math.sin(a) * r * 0.58), value, font=_font(int(r * 0.15), False),
               fill=DIM, anchor="mm")
    d.text((cx, cy + r * 0.62), "KM/H", font=_font(int(r * 0.15)), fill=GREEN, anchor="mm")
    return img, (cx, cy, r)


def frame(cfg: Overlay, size: tuple[int, int], speed_kmh: float, alt_m: float, vario_ms: float,
          _cache: dict = {}) -> Image.Image:
    key = (cfg.model_dump_json(), size)
    if key not in _cache:
        _cache.clear()
        _cache[key] = _dial(cfg, size)
    base, (cx, cy, r) = _cache[key]
    w, h = size[0] * SS, size[1] * SS
    if cfg.speed:
        img = base.copy()
    else:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    text_x = h * 0.12
    if cfg.speed:
        ring = r * 0.80
        part = min(max(speed_kmh / cfg.speed_max_kmh, 0.0), 1.0)
        if part > 0.003:
            d.arc([cx - ring, cy - ring, cx + ring, cy + ring], SWEEP_FROM, SWEEP_FROM + SWEEP * part,
                  fill=GREEN, width=int(r * 0.11))
        a = math.radians(SWEEP_FROM + SWEEP * part)
        d.line([cx + math.cos(a) * r * 0.66, cy + math.sin(a) * r * 0.66,
                cx + math.cos(a) * r * 0.97, cy + math.sin(a) * r * 0.97], fill=WHITE, width=int(r * 0.05))
        d.text((cx, cy), f"{speed_kmh:.0f}", font=_font(int(r * 0.50)), fill=WHITE, anchor="mm",
               stroke_width=SS, stroke_fill=(0, 0, 0, 160))
        text_x = cx + r + h * 0.08
    y = h * 0.30
    if cfg.altitude:
        number = f"{alt_m:.0f}"
        big = _font(int(h * 0.30))
        d.text((text_x, y), number, font=big, fill=WHITE, anchor="ls", stroke_width=SS * 2, stroke_fill=(0, 0, 0, 150))
        d.text((text_x + d.textlength(number, font=big) + h * 0.03, y), "m", font=_font(int(h * 0.15)),
               fill=GREEN, anchor="ls", stroke_width=SS, stroke_fill=(0, 0, 0, 150))
        y += h * 0.15
        d.text((text_x, y), "Elevation", font=_font(int(h * 0.12), False), fill=WHITE, anchor="ls",
               stroke_width=SS, stroke_fill=(0, 0, 0, 150))
        y += h * 0.06
        d.rounded_rectangle([text_x, y, text_x + h * 1.0, y + h * 0.035], radius=h * 0.02, fill=GREEN)
        y += h * 0.26
    if cfg.vario:
        colour = GREEN if vario_ms >= 0 else (255, 120, 90, 255)
        d.text((text_x, y), f"{vario_ms:+.1f} m/s", font=_font(int(h * 0.16)), fill=colour, anchor="ls",
               stroke_width=SS * 2, stroke_fill=(0, 0, 0, 150))
    return img.resize(size, Image.LANCZOS)


def frames_rgba(cfg: Overlay, telemetry: Telemetry, frame_width: int, start_s: float, duration_s: float) -> bytes:
    """Raw RGBA frames at cfg.fps for one shot; empty when the telemetry does not cover it."""
    sampler = Sampler(telemetry)
    if not cfg.enabled or not sampler.covers(start_s, start_s + duration_s):
        return b""
    size = canvas_size(cfg, frame_width)
    out = bytearray()
    for i in range(max(int(math.ceil(duration_s * cfg.fps)), 1)):
        out += frame(cfg, size, *sampler.at(start_s + i / cfg.fps)).tobytes()
    return bytes(out)

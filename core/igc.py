"""IGC flight log parser (B records only)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Fix:
    t: float      # unix time, UTC
    lat: float
    lon: float
    alt: float    # metres; GPS altitude, pressure altitude when GPS is zero


_DATE = re.compile(r"^HFDTE(?:DATE:)?\s*(\d{2})(\d{2})(\d{2})")
_B = re.compile(r"^B(\d{2})(\d{2})(\d{2})(\d{2})(\d{5})([NS])(\d{3})(\d{5})([EW])([AV])([-\d]\d{4})([-\d]\d{4})")


def parse_igc(path: Path) -> list[Fix]:
    date: datetime | None = None
    fixes: list[Fix] = []
    last_tod = -1
    for line in Path(path).read_text(encoding="latin-1").splitlines():
        if line.startswith("HFDTE"):
            m = _DATE.match(line)
            if m:
                d, mo, y = (int(g) for g in m.groups())
                date = datetime(2000 + y, mo, d, tzinfo=timezone.utc)
            continue
        m = _B.match(line)
        if not m:
            continue
        if date is None:
            raise ValueError(f"{path}: B record before the HFDTE date header")
        hh, mm, ss = int(m[1]), int(m[2]), int(m[3])
        tod = hh * 3600 + mm * 60 + ss
        if tod < last_tod - 12 * 3600:      # log runs past midnight UTC
            date += timedelta(days=1)
        if tod <= last_tod and tod >= last_tod - 12 * 3600:
            continue                         # duplicate or out-of-order fix
        last_tod = tod
        lat = int(m[4]) + int(m[5]) / 60000.0
        lon = int(m[7]) + int(m[8]) / 60000.0
        if m[6] == "S":
            lat = -lat
        if m[9] == "W":
            lon = -lon
        press, gps = int(m[11]), int(m[12])
        fixes.append(Fix(date.timestamp() + tod, lat, lon, float(gps if gps else press)))
    if len(fixes) < 2:
        raise ValueError(f"{path}: no usable fixes")
    return fixes

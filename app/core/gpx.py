"""GPX parsing (stdlib xml), coverage analysis and windowing/resampling
for later GPS injection (CAMM track, cf. camm.py).

No external dependency (no gpxpy): ``xml.etree.ElementTree`` only.

Offset-correction model (important, read by camm.py):
    ``resample()`` always produces one point per ``rate_hz`` tick over the whole
    video window ``[0, duration_s]`` (time *relative to the video*, so the first
    point always corresponds to ``video_start_utc`` and the last to
    ``video_start_utc + duration_s``, whether or not there is GPX at the ends).
    The output point with index ``i`` is labeled ``t = video_start_utc + i/rate_hz``
    (this value, and not the raw GPX timestamp, is what is written into the
    CAMM packet as ``time_gps_epoch``: after correcting ``offset_s``, the
    displayed "GPS time" must coincide with the real instant of the video). The
    position (lat/lon/ele) at that instant is interpolated in the raw track at
    real time ``t + offset_s`` — it is ``offset_s`` that expresses the clock
    offset between the GPX and the video, not a PTS offset in the final file.
    Thus ``camm.py`` only needs ``video_start_utc`` (not the offset) to place
    each sample back on the video timeline: ``pts = t - video_start_utc``.
"""
from __future__ import annotations

import bisect
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

EARTH_RADIUS_M = 6371000.0


class GpxError(Exception):
    """Explicit GPX parsing/processing error."""


@dataclass
class GpxPoint:
    t: datetime
    lat: float
    lon: float
    ele: float | None
    speed: float | None


def _localname(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _parse_gpx_time(text: str) -> datetime:
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise GpxError(f"unreadable GPX timestamp: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_gpx(path: str) -> list[GpxPoint]:
    """Parse a GPX file (trkpt only) and return the points sorted by
    increasing timestamp (UTC). ``trkpt`` without a ``<time>`` tag are ignored
    (cannot be placed on the video timeline)."""
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise GpxError(f"invalid GPX XML in {path}: {exc}") from exc
    except OSError as exc:
        raise GpxError(f"GPX file not found: {path}") from exc

    points: list[GpxPoint] = []
    for trkpt in tree.getroot().iter():
        if _localname(trkpt.tag) != "trkpt":
            continue
        lat_attr, lon_attr = trkpt.get("lat"), trkpt.get("lon")
        if lat_attr is None or lon_attr is None:
            continue
        lat, lon = float(lat_attr), float(lon_attr)
        ele: float | None = None
        speed: float | None = None
        t: datetime | None = None
        for child in trkpt:
            name = _localname(child.tag)
            if name == "ele" and child.text:
                ele = float(child.text)
            elif name == "time" and child.text:
                t = _parse_gpx_time(child.text)
            elif name == "speed" and child.text:
                speed = float(child.text)
            elif name == "extensions":
                for ext in child.iter():
                    if _localname(ext.tag) == "speed" and ext.text:
                        speed = float(ext.text)
        if t is None:
            continue
        points.append(GpxPoint(t=t, lat=lat, lon=lon, ele=ele, speed=speed))

    points.sort(key=lambda p: p.t)
    return points


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def analyze(points: list[GpxPoint], video_start_utc: datetime, duration_s: float) -> dict:
    """Assess the GPX coverage of the video window ``[video_start_utc,
    video_start_utc+duration_s]`` at zero offset, for UI display/slider."""
    window_start = video_start_utc
    window_end = video_start_utc + timedelta(seconds=duration_s)

    n_points_in_window = sum(1 for p in points if window_start <= p.t <= window_end)

    if points:
        overlap_start = max(window_start, points[0].t)
        overlap_end = min(window_end, points[-1].t)
        overlap_s = max(0.0, (overlap_end - overlap_start).total_seconds())
    else:
        overlap_s = 0.0

    coverage_pct = (overlap_s / duration_s * 100.0) if duration_s > 0 else 0.0

    gaps_gt_5s = []
    for a, b in zip(points, points[1:]):
        dt = (b.t - a.t).total_seconds()
        if dt > 5.0:
            gaps_gt_5s.append({
                "start": a.t.isoformat(),
                "end": b.t.isoformat(),
                "duration_s": dt,
            })

    if not points:
        suggested_offset_s = 0.0
    elif coverage_pct >= 90.0:
        suggested_offset_s = 0.0
    else:
        # Shift the window to make it coincide with the start of the GPX track.
        suggested_offset_s = (points[0].t - video_start_utc).total_seconds()

    return {
        "overlap_s": overlap_s,
        "coverage_pct": coverage_pct,
        "gaps_gt_5s": gaps_gt_5s,
        "n_points_in_window": n_points_in_window,
        "suggested_offset_s": suggested_offset_s,
    }


def resample(
    points: list[GpxPoint],
    video_start_utc: datetime,
    duration_s: float,
    offset_s: float,
    rate_hz: float = 1.0,
) -> list[GpxPoint]:
    """Resample the raw GPX track over the whole video window, at ``rate_hz``.

    Real windowing in the GPX: ``[video_start_utc+offset_s,
    video_start_utc+offset_s+duration_s]``. The output point ``i`` is labeled
    at *video* time ``video_start_utc + i/rate_hz`` (see the module docstring)
    and interpolated linearly in the raw track at the corresponding real time
    (``+ offset_s``). Outside the GPX track, the position is held
    (constant extrapolation) rather than guessed.
    """
    if not points or duration_s <= 0 or rate_hz <= 0:
        return []

    n = max(1, math.floor(duration_s * rate_hz) + 1)
    times = [p.t for p in points]

    def interpolate(real_t: datetime) -> tuple[float, float, float | None]:
        if real_t <= times[0]:
            p = points[0]
            return p.lat, p.lon, p.ele
        if real_t >= times[-1]:
            p = points[-1]
            return p.lat, p.lon, p.ele
        idx = bisect.bisect_right(times, real_t) - 1
        a, b = points[idx], points[idx + 1]
        span = (b.t - a.t).total_seconds()
        frac = (real_t - a.t).total_seconds() / span if span > 0 else 0.0
        lat = a.lat + (b.lat - a.lat) * frac
        lon = a.lon + (b.lon - a.lon) * frac
        ele = None
        if a.ele is not None and b.ele is not None:
            ele = a.ele + (b.ele - a.ele) * frac
        return lat, lon, ele

    result: list[GpxPoint] = []
    prev_lat = prev_lon = None
    prev_t_rel = None
    for i in range(n):
        t_rel = min(i / rate_hz, duration_s)
        video_t = video_start_utc + timedelta(seconds=t_rel)
        real_t = video_t + timedelta(seconds=offset_s)
        lat, lon, ele = interpolate(real_t)
        speed = None
        if prev_lat is not None:
            dt = t_rel - prev_t_rel
            if dt > 0:
                dist = _haversine_m(prev_lat, prev_lon, lat, lon)
                speed = dist / dt
        result.append(GpxPoint(t=video_t, lat=lat, lon=lon, ele=ele, speed=speed))
        prev_lat, prev_lon, prev_t_rel = lat, lon, t_rel

    if len(result) > 1 and result[0].speed is None:
        result[0] = replace(result[0], speed=result[1].speed)
    return result

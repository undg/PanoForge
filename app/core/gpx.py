"""Parsing GPX (stdlib xml), analyse de couverture et fenêtrage/rééchantillonnage
pour l'injection GPS ultérieure (piste CAMM, cf. camm.py).

Aucune dépendance externe (pas de gpxpy) : ``xml.etree.ElementTree`` uniquement.

Modèle de correction d'offset (important, lu par camm.py) :
    ``resample()`` produit toujours un point par tick de ``rate_hz`` sur toute la
    fenêtre vidéo ``[0, duration_s]`` (temps *relatif à la vidéo*, donc le premier
    point correspond toujours à ``video_start_utc`` et le dernier à
    ``video_start_utc + duration_s``, qu'il y ait ou non du GPX aux extrémités).
    Le point de sortie d'indice ``i`` est étiqueté ``t = video_start_utc + i/rate_hz``
    (c'est cette valeur, et non l'horodatage brut du GPX, qui est écrite dans le
    paquet CAMM comme ``time_gps_epoch`` : après correction d'``offset_s``, le
    "temps GPS" affiché doit coïncider avec l'instant réel de la vidéo). La
    position (lat/lon/ele) à cet instant est interpolée dans la trace brute au
    temps réel ``t + offset_s`` — c'est ``offset_s`` qui exprime le décalage
    d'horloge entre le GPX et la vidéo, pas un décalage de PTS dans le fichier
    final. Ainsi ``camm.py`` n'a besoin que de ``video_start_utc`` (pas de
    l'offset) pour replacer chaque échantillon sur la timeline de la vidéo :
    ``pts = t - video_start_utc``.
"""
from __future__ import annotations

import bisect
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

EARTH_RADIUS_M = 6371000.0


class GpxError(Exception):
    """Erreur explicite de parsing/traitement GPX."""


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
        raise GpxError(f"horodatage GPX illisible : {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_gpx(path: str) -> list[GpxPoint]:
    """Parse un fichier GPX (trkpt uniquement) et retourne les points triés par
    horodatage croissant (UTC). Les ``trkpt`` sans balise ``<time>`` sont ignorés
    (impossible à situer sur la timeline vidéo)."""
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise GpxError(f"XML GPX invalide dans {path} : {exc}") from exc
    except OSError as exc:
        raise GpxError(f"fichier GPX introuvable : {path}") from exc

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
    """Évalue la couverture GPX de la fenêtre vidéo ``[video_start_utc,
    video_start_utc+duration_s]`` à offset nul, pour affichage/curseur côté UI."""
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
        # Décale la fenêtre pour la faire coïncider avec le début de la trace GPX.
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
    """Rééchantillonne la trace GPX brute sur toute la fenêtre vidéo, à ``rate_hz``.

    Fenêtrage réel dans le GPX : ``[video_start_utc+offset_s,
    video_start_utc+offset_s+duration_s]``. Le point de sortie ``i`` est étiqueté
    au temps *vidéo* ``video_start_utc + i/rate_hz`` (voir docstring du module) et
    interpolé linéairement dans la trace brute au temps réel correspondant
    (``+ offset_s``). En dehors de la trace GPX, la position est maintenue
    (extrapolation constante) plutôt que devinée.
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

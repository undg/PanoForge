"""Photo extraction from 360 videos/photos (see SPEC.md, section
"360 photo extraction").

Three sources:
  - converted 360° MP4: accurate seek (``-ss`` before ``-i``) then 1 frame;
  - raw .OSV: calibrated stitching of ONE full resolution frame reusing
    ``maps.generate_remap_maps`` + the remap/maskedmerge graph from stitch.py
    (v360 baseline fallback if no calibration) — maps are cached on disk per
    (file, resolution);
  - camera 360° JPEG (equirect 2:1): used as is.

Reprojections (ffmpeg v360, input=e): ``flat`` (perspective without stretching,
v_fov computed from h_fov and the ratio), ``cylindrical`` (full 360° turn),
``equirect360`` (equirect 2:1 + GPano XMP injected for an interactive spherical
photo), ``littleplanet`` (stereographic looking down).

.LRF note: the camera writes a low resolution ``.LRF`` file next to each
``.OSV``. It could serve as a fast source for ``nav_proxy`` (if its content is
indeed a light dual-fisheye) but could not be tested (not copied with the
sample): the proxy is therefore always generated from the OSV itself.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess

# Ratios allowed for the "flat" projection (width:height).
RATIOS: dict[str, tuple[int, int]] = {
    "16:9": (16, 9), "21:9": (21, 9), "32:9": (32, 9),
    "4:3": (4, 3), "1:1": (1, 1), "9:16": (9, 16),
}

# v360 baseline filter (identical to stitch.py v360 mode).
_V360_BASE = ("v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:yaw=90:"
              "w={w}:h={h}:interp={interp}")

XMP_MARKER = b"http://ns.adobe.com/xap/1.0/\x00"


class PhotoError(Exception):
    """Explicit photo extraction error (intended for the API: 400/422)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _even(n: float) -> int:
    v = int(round(n))
    return v if v % 2 == 0 else v + 1


def _run(cmd: list[str], timeout: int = 120, what: str = "ffmpeg") -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise PhotoError(f"{cmd[0]} not found on the system") from exc
    except subprocess.TimeoutExpired as exc:
        raise PhotoError(f"{what}: timeout") from exc
    if proc.returncode != 0:
        raise PhotoError(f"{what} failed: {proc.stderr.strip()[-500:]}")


def probe_dims(path: str) -> tuple[int, int]:
    """(width, height) of the first video/image stream."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-print_format", "json", path],
            capture_output=True, text=True, timeout=30)
        s = json.loads(proc.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except (subprocess.TimeoutExpired, KeyError, IndexError, ValueError,
            json.JSONDecodeError, FileNotFoundError) as exc:
        raise PhotoError(f"unreadable dimensions: {path}") from exc


def _jpeg_q(quality: int) -> str:
    """quality 1..100 -> mjpeg -q:v scale (2 = almost lossless, 31 = worst)."""
    return str(max(2, min(31, round((100 - int(quality)) * 31 / 100))))


def _source_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".osv":
        return "osv"
    if ext in (".mp4", ".mov", ".m4v"):
        return "mp4"
    if ext in (".jpg", ".jpeg"):
        return "jpg"
    raise PhotoError(f"unsupported source type: {ext or path}")


def default_out_w(source_path: str) -> int:
    """Max equirect width of the source (default for out_w on the API side)."""
    kind = _source_kind(source_path)
    w, h = probe_dims(source_path)
    if kind == "osv":
        return 2 * w  # two 3840 fisheyes -> 7680 equirect
    return w


# ---------------------------------------------------------------------------
# Remap maps cache (per file + resolution)
# ---------------------------------------------------------------------------

def _maps_cache_dir() -> str:
    from app import config
    d = str(config.cache_dir() / "maps")
    os.makedirs(d, exist_ok=True)
    return d


def _cached_mapset(osv_path: str, out_w: int):
    """MapSet (possibly uncalibrated) from the disk cache, otherwise generated.

    Cache key = (path, mtime, resolution). Returns None if even the
    fallback could not be produced (modules missing...)."""
    try:
        from app.core.maps import MapSet, generate_remap_maps
    except ImportError:
        return None

    try:
        mtime = os.path.getmtime(osv_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{osv_path}:{mtime}:{out_w}".encode()).hexdigest()
    cdir = os.path.join(_maps_cache_dir(), key)
    meta_path = os.path.join(cdir, "meta.json")

    if os.path.isfile(meta_path):
        try:
            meta = json.loads(open(meta_path, encoding="utf-8").read())
            if all(os.path.isfile(p) for p in
                   meta["xmaps"] + meta["ymaps"] + [meta["blend_mask"]]):
                return MapSet(**meta)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

    # (Re)generation: calibration extracted from the OSV, then maps.
    from app.core import osv as osv_mod
    os.makedirs(cdir, exist_ok=True)
    try:
        meta_ex = osv_mod.extract_metadata(osv_path, cdir)
        calibration = meta_ex.get("calibration")
    except osv_mod.OsvError:
        calibration = None
    ms = generate_remap_maps(calibration, out_w, out_w // 2, cdir)
    try:
        with open(meta_path, "w", encoding="utf-8") as fp:
            json.dump({"out_w": ms.out_w, "out_h": ms.out_h, "xmaps": ms.xmaps,
                       "ymaps": ms.ymaps, "blend_mask": ms.blend_mask,
                       "calibrated": ms.calibrated}, fp)
    except OSError:
        pass
    return ms


# ---------------------------------------------------------------------------
# 1 full resolution equirect frame
# ---------------------------------------------------------------------------

def get_equirect_frame(source_path: str, time_s: float, workdir: str,
                       out_w: int | None = None) -> str:
    """Full resolution equirectangular PNG (or source JPEG) at ``time_s``."""
    if not os.path.isfile(source_path):
        raise PhotoError(f"file not found: {source_path}")
    kind = _source_kind(source_path)
    os.makedirs(workdir, exist_ok=True)
    out_png = os.path.join(workdir, "equirect.png")

    if kind == "jpg":
        w, h = probe_dims(source_path)
        if h == 0 or abs(w / h - 2.0) > 0.04:
            raise PhotoError(
                f"photo {os.path.basename(source_path)} is not equirectangular "
                f"2:1 ({w}x{h}) — extraction failed")
        return source_path  # used as is

    # PNG with minimal compression: the 8K intermediate goes from ~11 s to ~6 s.
    png_fast = ["-compression_level", "1"]

    if kind == "mp4":
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, time_s):.3f}",
               "-i", source_path, "-map", "0:v:0", "-frames:v", "1",
               *png_fast, out_png]
        _run(cmd, timeout=120, what="MP4 frame extraction")
        if not os.path.isfile(out_png):
            raise PhotoError(f"no frame at t={time_s:.2f}s (duration exceeded?)")
        return out_png

    # OSV: stitching of one frame — calibrated if maps available, otherwise v360 baseline.
    w_fish, _ = probe_dims(source_path)
    ew = _even(out_w or 2 * w_fish)
    eh = ew // 2
    maps = _cached_mapset(source_path, ew)
    ss = ["-ss", f"{max(0.0, time_s):.3f}"]
    if maps is not None and maps.calibrated:
        cmd = ["ffmpeg", "-y", "-v", "error", *ss, "-i", source_path]
        for i in range(2):
            cmd += ["-i", maps.xmaps[i], "-i", maps.ymaps[i]]
        cmd += ["-i", maps.blend_mask]
        graph = (
            "[0:0]format=gbrp[b0];[b0][1:v][2:v]remap=fill=black[e0];"
            "[0:1]format=gbrp[b1];[b1][3:v][4:v]remap=fill=black[e1];"
            "[5:v]format=gbrp[mk];[e0][e1][mk]maskedmerge[mg];"
            "[mg]format=rgb24[v]"
        )
        cmd += ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1",
                *png_fast, out_png]
        _run(cmd, timeout=180, what="calibrated stitching of an OSV frame")
    else:
        graph = ("[0:0][0:1]hstack[s];[s]"
                 + _V360_BASE.format(w=ew, h=eh, interp="lanczos")
                 + ",format=rgb24[v]")
        cmd = ["ffmpeg", "-y", "-v", "error", *ss, "-i", source_path,
               "-filter_complex", graph, "-map", "[v]", "-frames:v", "1",
               *png_fast, out_png]
        _run(cmd, timeout=180, what="v360 stitching of an OSV frame")
    if not os.path.isfile(out_png):
        raise PhotoError(f"no frame at t={time_s:.2f}s (duration exceeded?)")
    return out_png


# ---------------------------------------------------------------------------
# Reprojection
# ---------------------------------------------------------------------------

def _parse_ratio(ratio: str) -> tuple[float, float]:
    """Preset (16:9...) or custom ratio "a:b" (a, b > 0, a/b in [0.2, 8])."""
    if ratio in RATIOS:
        return RATIOS[ratio]
    parts = str(ratio).split(":")
    try:
        if len(parts) != 2:
            raise ValueError
        a, b = float(parts[0]), float(parts[1])
    except ValueError:
        raise PhotoError(
            f"invalid ratio: \"{ratio}\" — preset ({', '.join(RATIOS)}) "
            "or form \"a:b\" with numeric a and b > 0 expected") from None
    if not (a > 0 and b > 0):
        raise PhotoError(f"invalid ratio: \"{ratio}\" — a and b must be > 0")
    if not (0.2 <= a / b <= 8.0):
        raise PhotoError(
            f"ratio out of bounds: \"{ratio}\" (a/b = {a / b:.3g}, allowed: 0.2 to 8)")
    return a, b


def _norm180(a: float) -> float:
    """Wrap an angle in degrees into [-180, 180) (v360 rejects out-of-bounds yaw)."""
    return ((float(a) + 180.0) % 360.0) - 180.0


# ---------------------------------------------------------------------------
# Angle convention: viewer (yaw/pitch/roll fields + blue frame) -> v360
# ---------------------------------------------------------------------------
#
# The yaw/pitch/roll fields (and "start yaw" / "rotation") are expressed
# in the WebGL viewer convention (viewer.js), i.e. what the full-frame
# preview shows the user. ffmpeg v360 uses a
# DIFFERENT convention; without conversion, the extracted photo does not match the blue frame
# (historical bug: yaw offset by 180°). The mapping below was measured
# empirically (see work/photofix/) by comparing, on the SAME equirect, the viewer
# render and the v360 render:
#
#   • flat (three.js sphere): the navigable sphere samples the equirect at
#     u = yaw/360 (yaw=0 → LEFT EDGE of the equirect), whereas v360 output=flat
#     aims at the CENTER (u = 0.5) at yaw=0. Hence a 180° yaw offset, without
#     mirroring. Pitch is identical (positive = upward on both sides).
#     Roll is INVERTED (the three.js camera applies rotateZ(-roll), i.e. an
#     image rotation opposite to that of v360).
#         yaw_v360 = yaw + 180;  pitch_v360 = pitch;  roll_v360 = -roll
#     (proof: work/photofix/sweep.jpg — the sunrise framed at yaw=-147.1
#      on the viewer side is indeed produced by v360 yaw=+32.9 = -147.1 + 180.)
#
#   • cylindrical (projpreview shader): the shader samples u = 0.5 + yaw/360,
#     SAME origin as v360 output=cylindrical. No offset.
#         yaw_v360 = yaw
#
#   • littleplanet (nadir stereographic shader): the shader aims at the nadir with an
#     azimuth lon = atan2(y,x) + rotation, of INVERSE CHIRALITY to v360 output=sg.
#     To reproduce the preview exactly a horizontal mirror (hflip) is needed and
#         yaw_v360 = rotation + 90 ,  roll_v360 = 0 ,  pitch = -90
#     (the hflip is added to the filter by reproject()). The "rotation" arrives in
#     the roll_deg parameter (see frontend), the eventual yaw_deg field is ignored.
#
#   • equirect360: no orientation.


def _flat_angles_v360(yaw: float, pitch: float, roll: float) -> tuple[float, float, float]:
    """(yaw, pitch, roll) v360 for the flat projection, from the viewer convention."""
    return _norm180(yaw + 180.0), _norm180(pitch), _norm180(-roll)


def _flat_dims_and_vfov(out_w: int, ratio: str, h_fov_deg: float) -> tuple[int, int, float]:
    rw, rh = _parse_ratio(ratio)
    w = _even(out_w)
    h = _even(w * rh / rw)
    h_fov = min(140.0, max(30.0, float(h_fov_deg)))
    # correct perspective (no stretching): v_fov = 2·atan(tan(h_fov/2)·h/w)
    v_fov = math.degrees(2.0 * math.atan(math.tan(math.radians(h_fov) / 2.0) * h / w))
    return w, h, v_fov


def reproject(equirect_path: str, projection: str, params: dict,
              out_jpg: str, quality: int = 95) -> tuple[int, int]:
    """Reprojects a complete equirect image to ``out_jpg`` (returns (w, h))."""
    if not os.path.isfile(equirect_path):
        raise PhotoError(f"equirect not found: {equirect_path}")
    src_w, src_h = probe_dims(equirect_path)
    out_w = _even(params.get("out_w") or src_w)
    yaw = float(params.get("yaw_deg") or 0.0)
    pitch = float(params.get("pitch_deg") or 0.0)
    roll = float(params.get("roll_deg") or 0.0)
    q = _jpeg_q(quality)

    os.makedirs(os.path.dirname(os.path.abspath(out_jpg)), exist_ok=True)

    if projection == "flat":
        w, h, v_fov = _flat_dims_and_vfov(out_w, params.get("ratio") or "16:9",
                                          float(params.get("h_fov_deg") or 90.0))
        h_fov = min(140.0, max(30.0, float(params.get("h_fov_deg") or 90.0)))
        # viewer convention -> v360 (see _flat_angles_v360 / comment above)
        v_yaw, v_pitch, v_roll = _flat_angles_v360(yaw, pitch, roll)
        vf = (f"v360=input=e:output=flat:yaw={v_yaw:g}:pitch={v_pitch:g}:roll={v_roll:g}:"
              f"h_fov={h_fov:g}:v_fov={v_fov:g}:w={w}:h={h}:interp=lanczos")
    elif projection == "cylindrical":
        v_span = min(150.0, max(10.0, float(params.get("v_span_deg") or 60.0)))
        w = _even(out_w)
        h = _even(w * math.tan(math.radians(v_span) / 2.0) / math.pi)
        # cylindrical: the preview shader shares v360's origin -> yaw unchanged
        vf = (f"v360=input=e:output=cylindrical:yaw={_norm180(yaw):g}:"
              f"h_fov=360:v_fov={v_span:g}:w={w}:h={h}:interp=lanczos")
    elif projection == "equirect360":
        w = _even(out_w)
        h = w // 2
        vf = f"scale={w}:{h}:flags=lanczos" if (w, h) != (src_w, src_h) else "null"
    elif projection == "littleplanet":
        # Fixed FOV = that of the WebGL preview (viewer.js PLANET_FOV_DEG = 250):
        # the preview shader does not expose it, so extraction must use
        # the same value for the photo to match the displayed frame.
        fov = 250.0
        w = h = _even(out_w if params.get("out_w") else min(src_w // 2, 4096))
        # looking down; the planet "rotation" arrives in roll_deg.
        # The preview shader has inverse chirality to v360 output=sg: hflip +
        # yaw_v360 = rotation + 90 reproduce the preview exactly (see comment).
        v_yaw = _norm180(roll + 90.0)
        vf = (f"v360=input=e:output=sg:pitch=-90:yaw={v_yaw:g}:roll=0:"
              f"h_fov={fov:g}:v_fov={fov:g}:w={w}:h={h}:interp=lanczos,hflip")
    else:
        raise PhotoError(
            f"unknown projection: {projection} "
            "(choices: flat, cylindrical, equirect360, littleplanet)")

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", equirect_path,
           "-vf", vf + ",format=yuvj420p", "-frames:v", "1", "-q:v", q, out_jpg]
    _run(cmd, timeout=120, what=f"reprojection {projection}")
    if not os.path.isfile(out_jpg):
        raise PhotoError(f"{projection} reprojection: no output produced")

    if projection == "equirect360":
        _inject_gpano(out_jpg, w, h)
    return w, h


# ---------------------------------------------------------------------------
# GPano XMP (interactive spherical photo)
# ---------------------------------------------------------------------------

def _gpano_packet(w: int, h: int) -> bytes:
    xmp = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="panoforge">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        ' xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"'
        ' GPano:ProjectionType="equirectangular"'
        ' GPano:UsePanoramaViewer="True"'
        f' GPano:CroppedAreaImageWidthPixels="{w}"'
        f' GPano:CroppedAreaImageHeightPixels="{h}"'
        f' GPano:FullPanoWidthPixels="{w}"'
        f' GPano:FullPanoHeightPixels="{h}"'
        ' GPano:CroppedAreaLeftPixels="0"'
        ' GPano:CroppedAreaTopPixels="0"/>'
        '</rdf:RDF></x:xmpmeta>'
        '<?xpacket end="w"?>'
    )
    return xmp.encode("utf-8")


def _inject_gpano(jpg_path: str, w: int, h: int) -> None:
    """Inserts the GPano XMP packet in APP1 after the existing APPn segments."""
    data = open(jpg_path, "rb").read()
    if data[:2] != b"\xff\xd8":
        raise PhotoError(f"{jpg_path} is not a JPEG")
    payload = XMP_MARKER + _gpano_packet(w, h)
    if len(payload) + 2 > 0xFFFF:
        raise PhotoError("XMP packet too large")
    seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

    # insertion position: after SOI and the already present APP0..APP15
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF and 0xE0 <= data[pos + 1] <= 0xEF:
        (ln,) = struct.unpack(">H", data[pos + 2:pos + 4])
        pos += 2 + ln
    with open(jpg_path, "wb") as fp:
        fp.write(data[:pos] + seg + data[pos:])


# ---------------------------------------------------------------------------
# Navigation proxy (choose the moment in an OSV)
# ---------------------------------------------------------------------------

def nav_proxy(source_path: str, cache_dir: str) -> str:
    """~688 px equirect H.264 ultrafast proxy to navigate in an OSV."""
    if not os.path.isfile(source_path):
        raise PhotoError(f"file not found: {source_path}")
    if _source_kind(source_path) != "osv":
        raise PhotoError("nav_proxy only applies to .OSV files")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        mtime = os.path.getmtime(source_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{source_path}:{mtime}:navproxy".encode()).hexdigest()
    out = os.path.join(cache_dir, f"{key}_nav.mp4")
    if os.path.isfile(out):
        return out
    graph = ("[0:0][0:1]hstack[s];[s]"
             + _V360_BASE.format(w=688, h=344, interp="line")
             + ",format=yuv420p[v]")
    tmp = out + ".part"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", source_path,
           "-filter_complex", graph, "-map", "[v]", "-an",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
           "-movflags", "+faststart", "-f", "mp4", tmp]
    _run(cmd, timeout=300, what="navigation proxy generation")
    os.replace(tmp, out)
    return out

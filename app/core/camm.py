"""Muxing a CAMM track (Camera Motion Metadata, GPS type 6 packets) into an
existing MP4, without external dependencies (manual parsing/writing of MP4
boxes).

Strategy (cf. SPEC.md — do not reproduce the trek-view/telemetry-injector bug
that assumes the GPX starts exactly with the video):
  1. The sample bytes of existing tracks are NEVER moved.
     ``moov`` is extracted from its original position then rewritten at the very
     end of the file; all ``stco``/``co64`` offsets of existing tracks are
     corrected by a constant delta (± ``moov`` size) depending on whether they
     were located before or after ``moov`` originally — this works whatever the
     input layout (moov before or after mdat).
  2. The GPS samples of the new CAMM track are written into a fresh ``mdat``
     box, inserted just before this rewritten ``moov``.
  3. A complete new ``trak`` (tkhd/mdia/minf/stbl) is added as the last child
     of ``moov``, referencing these samples via ``stco``/``co64``.

Time alignment: each ``GpxPoint`` in ``samples`` (already resampled by
``gpx.resample``) carries a ``.t`` expressed on the same clock as
``video_start_utc`` — the CAMM PTS of sample i is simply
``(samples[i].t - video_start_utc)``, converted to the track's timescale.
"""
from __future__ import annotations

import math
import struct
from datetime import datetime, timezone

from app.core.gpx import GpxPoint

CAMM_TIMESCALE = 90000  # fine timescale (cf. SPEC.md)

# Default values in the absence of accuracy announced in the GPX (no dedicated
# field in GpxPoint): plausible "consumer GPS" accuracies.
DEFAULT_H_ACC = 5.0
DEFAULT_V_ACC = 8.0
DEFAULT_SPEED_ACC = 1.0


class CammError(Exception):
    """Explicit error during MP4 parsing/rewriting for the CAMM track."""


# ---------------------------------------------------------------------------
# MP4 box primitives (read)
# ---------------------------------------------------------------------------

def _read_box_header(buf: bytes, off: int) -> tuple[bytes, int, int]:
    if off + 8 > len(buf):
        raise CammError(f"truncated MP4 box at offset {off}")
    size = int.from_bytes(buf[off:off + 4], "big")
    typ = bytes(buf[off + 4:off + 8])
    hdr = 8
    if size == 1:
        size = int.from_bytes(buf[off + 8:off + 16], "big")
        hdr = 16
    elif size == 0:
        size = len(buf) - off
    if size < hdr:
        raise CammError(f"invalid MP4 box size at offset {off}")
    return typ, size, hdr


def _iter_top_boxes(buf: bytes) -> list[tuple[bytes, int, int, int]]:
    boxes = []
    off, n = 0, len(buf)
    while off + 8 <= n:
        typ, size, hdr = _read_box_header(buf, off)
        boxes.append((typ, off, size, hdr))
        off += size
    return boxes


def _find_direct_child(buf: bytes, start: int, end: int, target: bytes):
    off = start
    while off + 8 <= end:
        typ, size, hdr = _read_box_header(buf, off)
        if typ == target:
            return off, size, hdr
        off += size
    return None


_CONTAINER_TYPES = {b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


def _patch_sample_offsets(buf: bytearray, start: int, end: int, moov_file_off: int, moov_file_size: int) -> None:
    """Patch the stco/co64 tables in place after extracting moov from its
    original position (moov_file_off/size = position/size BEFORE extraction)."""
    off = start
    while off + 8 <= end:
        typ, size, hdr = _read_box_header(buf, off)
        body = off + hdr
        if typ == b"stco":
            count = int.from_bytes(buf[body + 4:body + 8], "big")
            base = body + 8
            for i in range(count):
                p = base + 4 * i
                v = int.from_bytes(buf[p:p + 4], "big")
                if v > moov_file_off:
                    v -= moov_file_size
                struct.pack_into(">I", buf, p, v)
        elif typ == b"co64":
            count = int.from_bytes(buf[body + 4:body + 8], "big")
            base = body + 8
            for i in range(count):
                p = base + 8 * i
                v = int.from_bytes(buf[p:p + 8], "big")
                if v > moov_file_off:
                    v -= moov_file_size
                struct.pack_into(">Q", buf, p, v)
        elif typ in _CONTAINER_TYPES:
            _patch_sample_offsets(buf, body, off + size, moov_file_off, moov_file_size)
        off += size


def _relocate_moov_to_end(data: bytes) -> tuple[bytearray, int, int, int]:
    top = _iter_top_boxes(data)
    moov = next((b for b in top if b[0] == b"moov"), None)
    if moov is None:
        raise CammError("box 'moov' not found in the input MP4")
    _, moov_off, moov_size, moov_hdr = moov

    out = bytearray()
    for typ, off, size, _hdr in top:
        if typ == b"moov":
            continue
        out += data[off:off + size]

    moov_bytes = bytearray(data[moov_off:moov_off + moov_size])
    _patch_sample_offsets(moov_bytes, moov_hdr, moov_size, moov_off, moov_size)

    new_moov_off = len(out)
    out += moov_bytes
    return out, new_moov_off, moov_size, moov_hdr


# ---------------------------------------------------------------------------
# Box construction (write)
# ---------------------------------------------------------------------------

def _u32(v: int) -> bytes:
    return struct.pack(">I", v & 0xFFFFFFFF)


def _u16(v: int) -> bytes:
    return struct.pack(">H", v & 0xFFFF)


def _i32(v: int) -> bytes:
    return struct.pack(">i", v)


def _box(typ: bytes, body: bytes) -> bytes:
    return _u32(8 + len(body)) + typ + body


_IDENTITY_MATRIX = (
    _i32(0x00010000) + _i32(0) + _i32(0)
    + _i32(0) + _i32(0x00010000) + _i32(0)
    + _i32(0) + _i32(0) + _i32(0x40000000)
)


def _build_tkhd(track_id: int, duration_movie_ts: int) -> bytes:
    body = (
        b"\x00\x00\x00\x07"  # version 0, flags: enabled|in_movie|in_preview
        + _u32(0) + _u32(0)  # creation/modification time
        + _u32(track_id)
        + _u32(0)  # reserved
        + _u32(duration_movie_ts)
        + _u32(0) + _u32(0)  # reserved
        + _u16(0) + _u16(0)  # layer, alternate_group
        + _u16(0) + _u16(0)  # volume (non-audio track), reserved
        + _IDENTITY_MATRIX
        + _u32(0) + _u32(0)  # width/height (16.16, non-visual track)
    )
    return _box(b"tkhd", body)


def _build_mdhd(timescale: int, duration: int) -> bytes:
    body = (
        b"\x00\x00\x00\x00"
        + _u32(0) + _u32(0)  # creation/modification time
        + _u32(timescale)
        + _u32(duration)
        + _u16(0x55C4)  # language "und"
        + _u16(0)
    )
    return _box(b"mdhd", body)


def _build_hdlr(handler_type: bytes, name: bytes) -> bytes:
    body = (
        b"\x00\x00\x00\x00"
        + _u32(0)  # pre_defined
        + handler_type
        + _u32(0) + _u32(0) + _u32(0)  # reserved
        + name + b"\x00"
    )
    return _box(b"hdlr", body)


def _build_nmhd() -> bytes:
    return _box(b"nmhd", b"\x00\x00\x00\x00")


def _build_dinf() -> bytes:
    url_box = _box(b"url ", b"\x00\x00\x00\x01")  # flags=1: self-contained
    dref_body = b"\x00\x00\x00\x00" + _u32(1) + url_box
    return _box(b"dinf", _box(b"dref", dref_body))


def _build_stsd_camm() -> bytes:
    entry_body = b"\x00" * 6 + _u16(1)  # reserved(6) + data_reference_index=1
    entry = _box(b"camm", entry_body)
    body = b"\x00\x00\x00\x00" + _u32(1) + entry
    return _box(b"stsd", body)


def _build_stts(runs: list[tuple[int, int]]) -> bytes:
    body = b"\x00\x00\x00\x00" + _u32(len(runs))
    for count, dur in runs:
        body += _u32(count) + _u32(dur)
    return _box(b"stts", body)


def _build_stsc(n_samples: int) -> bytes:
    body = b"\x00\x00\x00\x00" + _u32(1) + _u32(1) + _u32(n_samples) + _u32(1)
    return _box(b"stsc", body)


def _build_stsz(sample_size: int, count: int) -> bytes:
    body = b"\x00\x00\x00\x00" + _u32(sample_size) + _u32(count)
    return _box(b"stsz", body)


def _build_stco(offset: int) -> bytes:
    body = b"\x00\x00\x00\x00" + _u32(1) + _u32(offset)
    return _box(b"stco", body)


def _build_co64(offset: int) -> bytes:
    body = b"\x00\x00\x00\x00" + _u32(1) + struct.pack(">Q", offset)
    return _box(b"co64", body)


def _mdat_box(body: bytes) -> tuple[bytes, int]:
    """Return (box bytes, header size) — switches to
    largesize (16-byte header) if the content exceeds 4 GB (rare, untested)."""
    if 8 + len(body) > 0xFFFFFFFF:
        header = struct.pack(">I4sQ", 1, b"mdat", 16 + len(body))
        return header + body, 16
    return struct.pack(">I4s", 8 + len(body), b"mdat") + body, 8


def _read_mvhd(buf: bytes, off: int, hdr: int) -> tuple[int, int, int]:
    body = off + hdr
    version = buf[body]
    if version == 1:
        timescale = int.from_bytes(buf[body + 20:body + 24], "big")
        next_track_id_off = body + 4 + 104
    else:
        timescale = int.from_bytes(buf[body + 12:body + 16], "big")
        next_track_id_off = body + 4 + 92
    next_track_id = int.from_bytes(buf[next_track_id_off:next_track_id_off + 4], "big")
    return timescale, next_track_id, next_track_id_off


# ---------------------------------------------------------------------------
# CAMM GPS type 6 packet
# ---------------------------------------------------------------------------

def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def _velocity_enu(samples: list[GpxPoint], i: int, dt_s: float) -> tuple[float, float, float]:
    """Reconstruct (vel_e, vel_n, vel_up) from the bearing between neighboring
    points: the GPX has only a scalar speed (no bearing), so this is a
    reasonable approximation rather than a direct measurement (documented
    limitation)."""
    speed = samples[i].speed or 0.0
    if i + 1 < len(samples):
        a, b = samples[i], samples[i + 1]
    elif i > 0:
        a, b = samples[i - 1], samples[i]
    else:
        a = b = None

    vel_e = vel_n = 0.0
    if a is not None and (a.lat != b.lat or a.lon != b.lon) and speed:
        brg = math.radians(_bearing_deg(a.lat, a.lon, b.lat, b.lon))
        vel_e = speed * math.sin(brg)
        vel_n = speed * math.cos(brg)

    vel_up = 0.0
    if a is not None and a.ele is not None and b.ele is not None and dt_s > 0:
        vel_up = (b.ele - a.ele) / dt_s

    return vel_e, vel_n, vel_up


def _gps_packet(sample: GpxPoint, vel_e: float, vel_n: float, vel_up: float) -> bytes:
    t = sample.t if sample.t.tzinfo is not None else sample.t.replace(tzinfo=timezone.utc)
    epoch = t.timestamp()
    fix_type = 3 if sample.ele is not None else 2
    alt = sample.ele if sample.ele is not None else 0.0
    speed_acc = DEFAULT_SPEED_ACC
    return struct.pack(
        "<HHdiddfffffff",
        0, 6,
        epoch,
        fix_type,
        sample.lat, sample.lon,
        alt,
        DEFAULT_H_ACC, DEFAULT_V_ACC,
        vel_e, vel_n, vel_up,
        speed_acc,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inject_camm(mp4_in: str, mp4_out: str, samples: list[GpxPoint], video_start_utc: datetime) -> None:
    """Add a ``meta``/``camm`` track (GPS type 6 packets) to ``mp4_in`` and
    write the result to ``mp4_out``. ``samples`` must be non-empty and already
    resampled (cf. ``gpx.resample``); ``video_start_utc`` serves as the
    ``t=0`` reference for PTS computation (see the gpx.py docstring)."""
    if not samples:
        raise CammError("no GPS sample to inject (empty samples)")
    if video_start_utc.tzinfo is None:
        video_start_utc = video_start_utc.replace(tzinfo=timezone.utc)

    with open(mp4_in, "rb") as fp:
        data = fp.read()

    out, moov_off, moov_size, moov_hdr = _relocate_moov_to_end(data)
    moov_body_start = moov_off + moov_hdr
    moov_body_end = moov_off + moov_size

    mvhd = _find_direct_child(out, moov_body_start, moov_body_end, b"mvhd")
    if mvhd is None:
        raise CammError("box 'mvhd' not found in moov")
    mvhd_off, _mvhd_size, mvhd_hdr = mvhd
    movie_timescale, next_track_id, ntid_off = _read_mvhd(out, mvhd_off, mvhd_hdr)

    # --- PTS: strictly increasing CAMM ticks ---
    pts_s = [(s.t if s.t.tzinfo else s.t.replace(tzinfo=timezone.utc)) - video_start_utc for s in samples]
    ticks = [max(0, round(dt.total_seconds() * CAMM_TIMESCALE)) for dt in pts_s]
    for i in range(1, len(ticks)):
        if ticks[i] <= ticks[i - 1]:
            ticks[i] = ticks[i - 1] + 1

    if len(ticks) == 1:
        durations = [CAMM_TIMESCALE]
    else:
        durations = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
        durations.append(durations[-1])

    runs: list[tuple[int, int]] = []
    for d in durations:
        if runs and runs[-1][1] == d:
            runs[-1] = (runs[-1][0] + 1, d)
        else:
            runs.append((1, d))

    packets = []
    for i, s in enumerate(samples):
        dt_s = durations[i] / CAMM_TIMESCALE
        vel_e, vel_n, vel_up = _velocity_enu(samples, i, dt_s)
        packets.append(_gps_packet(s, vel_e, vel_n, vel_up))
    sample_bytes = b"".join(packets)
    sample_size = len(packets[0]) if packets else 0

    mdat_bytes, mdat_hdr = _mdat_box(sample_bytes)
    sample_data_off = moov_off + mdat_hdr  # absolute position in the FINAL file

    use_co64 = (sample_data_off + len(sample_bytes)) > 0xFFFFFFFF
    stco_box = _build_co64(sample_data_off) if use_co64 else _build_stco(sample_data_off)

    stbl = _build_stsd_camm() + _build_stts(runs) + _build_stsc(len(samples)) + _build_stsz(sample_size, len(samples)) + stco_box
    minf = _build_nmhd() + _build_dinf() + _box(b"stbl", stbl)
    mdhd_duration = sum(durations)
    movie_duration_ts = round((mdhd_duration / CAMM_TIMESCALE) * movie_timescale)
    mdia = _build_mdhd(CAMM_TIMESCALE, mdhd_duration) + _build_hdlr(b"camm", b"CameraMetadataMotionHandler") + _box(b"minf", minf)
    trak_body = _build_tkhd(next_track_id, movie_duration_ts) + _box(b"mdia", mdia)
    trak_bytes = _box(b"trak", trak_body)

    # --- Final assembly: [non-moov...][CAMM mdat][moov + new trak] ---
    moov_section = out[moov_off:moov_off + moov_size]
    moov_section += trak_bytes
    new_moov_size = moov_size + len(trak_bytes)
    if moov_hdr == 16:
        struct.pack_into(">Q", moov_section, 8, new_moov_size)
    else:
        struct.pack_into(">I", moov_section, 0, new_moov_size)
    struct.pack_into(">I", moov_section, ntid_off - moov_off, next_track_id + 1)

    final_bytes = out[:moov_off] + mdat_bytes + moov_section

    with open(mp4_out, "wb") as fp:
        fp.write(final_bytes)

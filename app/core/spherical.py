"""Injection of spherical video metadata V1 (``uuid`` box with GSpherical XML)
and V2 (``sv3d``/``proj``/``equi``) into the video track of an MP4, without
external dependency (see google/spatial-media: docs/spherical-video-rfc.md and
spherical-video-v2-rfc.md for the binary layout).

Placement (per the RFCs):
  - V1: ``uuid`` box (extended type ``ffcc8263-f855-4a93-8814-587a02521fdd``,
    content = UTF-8 XML) added as the **last child of the video ``trak``**
    (not at the file root level).
  - V2: ``sv3d`` box (containing ``svhd`` + ``proj``{``prhd``+``equi``})
    added as the **last child of the video sample entry** (in
    ``stsd``, after the usual boxes such as ``hvcC``/``avcC``/``colr``).

As for camm.py: ``moov`` is first extracted from its original position and
rewritten at the end of the file (the stco/co64 offsets of existing tracks are
corrected accordingly), which then allows ``moov`` to grow freely (insertions
above) without ever touching the data of existing tracks.
"""
from __future__ import annotations

import struct

from app.core.gpx import GpxPoint, resample

V1_UUID = bytes.fromhex("ffcc8263f8554a938814587a02521fdd")

V1_XML_TEMPLATE = (
    '<?xml version="1.0"?>'
    '<rdf:SphericalVideo\n'
    ' xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
    ' xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/">\n'
    ' <GSpherical:Spherical>true</GSpherical:Spherical>\n'
    ' <GSpherical:Stitched>true</GSpherical:Stitched>\n'
    ' <GSpherical:StitchingSoftware>Osmo360Studio</GSpherical:StitchingSoftware>\n'
    ' <GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>\n'
    '</rdf:SphericalVideo>\n'
)

_VIDEO_SAMPLE_ENTRY_TYPES = {
    b"avc1", b"avc3", b"hvc1", b"hev1", b"mp4v", b"apcn", b"apch", b"vp09", b"av01",
}


class SphericalError(Exception):
    """Explicit error during MP4 parsing/rewriting for spherical metadata."""


# ---------------------------------------------------------------------------
# MP4 box primitives (read) — same principles as camm.py
# ---------------------------------------------------------------------------

def _read_box_header(buf: bytes, off: int) -> tuple[bytes, int, int]:
    if off + 8 > len(buf):
        raise SphericalError(f"truncated MP4 box at offset {off}")
    size = int.from_bytes(buf[off:off + 4], "big")
    typ = bytes(buf[off + 4:off + 8])
    hdr = 8
    if size == 1:
        size = int.from_bytes(buf[off + 8:off + 16], "big")
        hdr = 16
    elif size == 0:
        size = len(buf) - off
    if size < hdr:
        raise SphericalError(f"invalid MP4 box size at offset {off}")
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


def _iter_direct_children(buf: bytes, start: int, end: int):
    off = start
    while off + 8 <= end:
        typ, size, hdr = _read_box_header(buf, off)
        yield typ, off, size, hdr
        off += size


_CONTAINER_TYPES = {b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


def _patch_sample_offsets(buf: bytearray, start: int, end: int, moov_file_off: int, moov_file_size: int) -> None:
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
        raise SphericalError("'moov' box not found in the input MP4")
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


def _grow_box(buf: bytearray, off: int, hdr: int, old_size: int, delta: int) -> None:
    """Adds ``delta`` to the (already stored) size of a box at ``off``."""
    new_size = old_size + delta
    if hdr == 16:
        struct.pack_into(">Q", buf, off + 8, new_size)
    else:
        struct.pack_into(">I", buf, off, new_size)


# ---------------------------------------------------------------------------
# Building the V1/V2 boxes
# ---------------------------------------------------------------------------

def _u32(v: int) -> bytes:
    return struct.pack(">I", v & 0xFFFFFFFF)


def _box(typ: bytes, body: bytes) -> bytes:
    return _u32(8 + len(body)) + typ + body


def _fullbox(typ: bytes, version: int, flags: int, body: bytes) -> bytes:
    return _box(typ, bytes([version]) + flags.to_bytes(3, "big") + body)


def _build_svhd(metadata_source: bytes = b"Osmo360Studio") -> bytes:
    return _fullbox(b"svhd", 0, 0, metadata_source + b"\x00")


def _build_prhd() -> bytes:
    # yaw/pitch/roll = 0 (16.16 fixed): neutral orientation, full sphere.
    return _fullbox(b"prhd", 0, 0, struct.pack(">iii", 0, 0, 0))


def _build_equi() -> bytes:
    # projection_bounds top/bottom/left/right = 0 (0.32 fixed): no cropping,
    # full sphere ("full sphere" values requested).
    return _fullbox(b"equi", 0, 0, struct.pack(">IIII", 0, 0, 0, 0))


def _build_proj() -> bytes:
    return _box(b"proj", _build_prhd() + _build_equi())


def _build_sv3d() -> bytes:
    return _box(b"sv3d", _build_svhd() + _build_proj())


def _build_v1_uuid() -> bytes:
    xml_bytes = V1_XML_TEMPLATE.encode("utf-8")
    return _box(b"uuid", V1_UUID + xml_bytes)


# ---------------------------------------------------------------------------
# Locating the video track
# ---------------------------------------------------------------------------

def _find_video_trak(buf: bytes, moov_body_start: int, moov_body_end: int):
    """Returns (trak_off, trak_size, trak_hdr) of the first track whose
    mdia/hdlr handler is 'vide'."""
    for typ, off, size, hdr in _iter_direct_children(buf, moov_body_start, moov_body_end):
        if typ != b"trak":
            continue
        mdia = _find_direct_child(buf, off + hdr, off + size, b"mdia")
        if mdia is None:
            continue
        mdia_off, mdia_size, mdia_hdr = mdia
        hdlr = _find_direct_child(buf, mdia_off + mdia_hdr, mdia_off + mdia_size, b"hdlr")
        if hdlr is None:
            continue
        hdlr_off, _hdlr_size, hdlr_hdr = hdlr
        # hdlr body: version/flags(4) + pre_defined(4) + handler_type(4) + ...
        handler_type = bytes(buf[hdlr_off + hdlr_hdr + 8:hdlr_off + hdlr_hdr + 12])
        if handler_type == b"vide":
            return off, size, hdr
    raise SphericalError("no video track (handler 'vide') found in moov")


def _find_sample_entry(buf: bytes, trak_off: int, trak_size: int, trak_hdr: int):
    mdia = _find_direct_child(buf, trak_off + trak_hdr, trak_off + trak_size, b"mdia")
    if mdia is None:
        raise SphericalError("'mdia' box not found in the video track")
    mdia_off, mdia_size, mdia_hdr = mdia
    minf = _find_direct_child(buf, mdia_off + mdia_hdr, mdia_off + mdia_size, b"minf")
    if minf is None:
        raise SphericalError("'minf' box not found")
    minf_off, minf_size, minf_hdr = minf
    stbl = _find_direct_child(buf, minf_off + minf_hdr, minf_off + minf_size, b"stbl")
    if stbl is None:
        raise SphericalError("'stbl' box not found")
    stbl_off, stbl_size, stbl_hdr = stbl
    stsd = _find_direct_child(buf, stbl_off + stbl_hdr, stbl_off + stbl_size, b"stsd")
    if stsd is None:
        raise SphericalError("'stsd' box not found")
    stsd_off, stsd_size, stsd_hdr = stsd
    # stsd body: version/flags(4) + entry_count(4), then the 1st entry.
    entry_off = stsd_off + stsd_hdr + 8
    entry_typ, entry_size, entry_hdr = _read_box_header(buf, entry_off)
    if entry_typ not in _VIDEO_SAMPLE_ENTRY_TYPES:
        raise SphericalError(f"unexpected video sample entry: {entry_typ!r}")
    ancestors = [
        (mdia_off, mdia_hdr, mdia_size),
        (minf_off, minf_hdr, minf_size),
        (stbl_off, stbl_hdr, stbl_size),
        (stsd_off, stsd_hdr, stsd_size),
        (entry_off, entry_hdr, entry_size),
    ]
    return ancestors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inject_spherical(mp4_in: str, mp4_out: str) -> None:
    """Injects spherical metadata V1 (uuid GSpherical) + V2 (sv3d) into
    the video track of ``mp4_in``, as full-frame monoscopic equirectangular,
    and writes the result to ``mp4_out``."""
    with open(mp4_in, "rb") as fp:
        data = fp.read()

    out, moov_off, moov_size, moov_hdr = _relocate_moov_to_end(data)
    moov_body_start = moov_off + moov_hdr
    moov_body_end = moov_off + moov_size

    trak_off, trak_size, trak_hdr = _find_video_trak(out, moov_body_start, moov_body_end)
    ancestors = _find_sample_entry(out, trak_off, trak_size, trak_hdr)
    # ancestors = [mdia, minf, stbl, stsd, entry]; we add trak and moov, which
    # necessarily also enclose the insertion point.
    full_chain = [(trak_off, trak_hdr, trak_size), (moov_off, moov_hdr, moov_size)] + ancestors

    entry_off, entry_hdr, entry_size = ancestors[-1]
    sv3d_bytes = _build_sv3d()
    insert_pos = entry_off + entry_size
    out[insert_pos:insert_pos] = sv3d_bytes
    delta = len(sv3d_bytes)
    for box_off, box_hdr, box_size in full_chain:
        _grow_box(out, box_off, box_hdr, box_size, delta)

    # After the V2 growth, trak has grown by `delta`: recompute its end
    # to insert the V1 uuid as the very last child of trak.
    new_trak_size = trak_size + delta
    new_moov_size = moov_size + delta
    uuid_bytes = _build_v1_uuid()
    insert_pos2 = trak_off + new_trak_size
    out[insert_pos2:insert_pos2] = uuid_bytes
    delta2 = len(uuid_bytes)
    _grow_box(out, trak_off, trak_hdr, new_trak_size, delta2)
    _grow_box(out, moov_off, moov_hdr, new_moov_size, delta2)

    with open(mp4_out, "wb") as fp:
        fp.write(out)


def export_windowed_gpx(
    points: list[GpxPoint],
    video_start_utc,
    duration_s: float,
    offset_s: float,
    out_path: str,
) -> None:
    """Writes a (side-car) GPX containing the resampled track (1 Hz) over
    the video window — useful for Street View Studio or manual verification."""
    samples = resample(points, video_start_utc, duration_s, offset_s, rate_hz=1.0)

    import xml.etree.ElementTree as ET

    gpx_el = ET.Element("gpx", {
        "version": "1.1",
        "creator": "Osmo360Studio",
        "xmlns": "http://www.topografix.com/GPX/1.1",
    })
    trk_el = ET.SubElement(gpx_el, "trk")
    ET.SubElement(trk_el, "name").text = "Osmo360Studio export"
    trkseg_el = ET.SubElement(trk_el, "trkseg")
    for s in samples:
        trkpt_el = ET.SubElement(trkseg_el, "trkpt", {"lat": f"{s.lat:.7f}", "lon": f"{s.lon:.7f}"})
        if s.ele is not None:
            ET.SubElement(trkpt_el, "ele").text = f"{s.ele:.2f}"
        ET.SubElement(trkpt_el, "time").text = s.t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if s.speed is not None:
            ext_el = ET.SubElement(trkpt_el, "extensions")
            ET.SubElement(ext_el, "speed").text = f"{s.speed:.3f}"

    tree = ET.ElementTree(gpx_el)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)

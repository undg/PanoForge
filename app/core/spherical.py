"""Injection des métadonnées vidéo sphériques V1 (boîte ``uuid`` XML GSpherical)
et V2 (``sv3d``/``proj``/``equi``) dans la piste vidéo d'un MP4, sans dépendance
externe (cf. google/spatial-media : docs/spherical-video-rfc.md et
spherical-video-v2-rfc.md pour le layout binaire).

Placement (d'après les RFC) :
  - V1 : boîte ``uuid`` (extended type ``ffcc8263-f855-4a93-8814-587a02521fdd``,
    contenu = XML UTF-8) ajoutée comme **dernier enfant du ``trak`` vidéo**
    (pas au niveau racine du fichier).
  - V2 : boîte ``sv3d`` (contenant ``svhd`` + ``proj``{``prhd``+``equi``})
    ajoutée comme **dernier enfant de l'entrée d'échantillon vidéo** (dans
    ``stsd``, après les boîtes habituelles type ``hvcC``/``avcC``/``colr``).

Comme pour camm.py : ``moov`` est d'abord extrait de sa position d'origine et
réécrit en fin de fichier (les décalages stco/co64 des pistes existantes sont
corrigés en conséquence), ce qui permet ensuite de faire grossir ``moov``
librement (insertions ci-dessus) sans jamais retoucher aux données des pistes
existantes.
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
    """Erreur explicite lors du parsing/réécriture MP4 pour les métadonnées sphériques."""


# ---------------------------------------------------------------------------
# Primitives boîtes MP4 (lecture) — mêmes principes que camm.py
# ---------------------------------------------------------------------------

def _read_box_header(buf: bytes, off: int) -> tuple[bytes, int, int]:
    if off + 8 > len(buf):
        raise SphericalError(f"boîte MP4 tronquée à l'offset {off}")
    size = int.from_bytes(buf[off:off + 4], "big")
    typ = bytes(buf[off + 4:off + 8])
    hdr = 8
    if size == 1:
        size = int.from_bytes(buf[off + 8:off + 16], "big")
        hdr = 16
    elif size == 0:
        size = len(buf) - off
    if size < hdr:
        raise SphericalError(f"taille de boîte MP4 invalide à l'offset {off}")
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
        raise SphericalError("boîte 'moov' introuvable dans le MP4 d'entrée")
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
    """Ajoute ``delta`` à la taille (déjà stockée) d'une boîte à ``off``."""
    new_size = old_size + delta
    if hdr == 16:
        struct.pack_into(">Q", buf, off + 8, new_size)
    else:
        struct.pack_into(">I", buf, off, new_size)


# ---------------------------------------------------------------------------
# Construction des boîtes V1/V2
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
    # yaw/pitch/roll = 0 (16.16 fixed) : orientation neutre, sphère complète.
    return _fullbox(b"prhd", 0, 0, struct.pack(">iii", 0, 0, 0))


def _build_equi() -> bytes:
    # projection_bounds top/bottom/left/right = 0 (0.32 fixed) : pas de recadrage,
    # sphère pleine (valeurs "pleine sphère" demandées).
    return _fullbox(b"equi", 0, 0, struct.pack(">IIII", 0, 0, 0, 0))


def _build_proj() -> bytes:
    return _box(b"proj", _build_prhd() + _build_equi())


def _build_sv3d() -> bytes:
    return _box(b"sv3d", _build_svhd() + _build_proj())


def _build_v1_uuid() -> bytes:
    xml_bytes = V1_XML_TEMPLATE.encode("utf-8")
    return _box(b"uuid", V1_UUID + xml_bytes)


# ---------------------------------------------------------------------------
# Localisation de la piste vidéo
# ---------------------------------------------------------------------------

def _find_video_trak(buf: bytes, moov_body_start: int, moov_body_end: int):
    """Retourne (trak_off, trak_size, trak_hdr) de la première piste dont le
    handler mdia/hdlr est 'vide'."""
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
        # hdlr body : version/flags(4) + pre_defined(4) + handler_type(4) + ...
        handler_type = bytes(buf[hdlr_off + hdlr_hdr + 8:hdlr_off + hdlr_hdr + 12])
        if handler_type == b"vide":
            return off, size, hdr
    raise SphericalError("aucune piste vidéo (handler 'vide') trouvée dans moov")


def _find_sample_entry(buf: bytes, trak_off: int, trak_size: int, trak_hdr: int):
    mdia = _find_direct_child(buf, trak_off + trak_hdr, trak_off + trak_size, b"mdia")
    if mdia is None:
        raise SphericalError("boîte 'mdia' introuvable dans la piste vidéo")
    mdia_off, mdia_size, mdia_hdr = mdia
    minf = _find_direct_child(buf, mdia_off + mdia_hdr, mdia_off + mdia_size, b"minf")
    if minf is None:
        raise SphericalError("boîte 'minf' introuvable")
    minf_off, minf_size, minf_hdr = minf
    stbl = _find_direct_child(buf, minf_off + minf_hdr, minf_off + minf_size, b"stbl")
    if stbl is None:
        raise SphericalError("boîte 'stbl' introuvable")
    stbl_off, stbl_size, stbl_hdr = stbl
    stsd = _find_direct_child(buf, stbl_off + stbl_hdr, stbl_off + stbl_size, b"stsd")
    if stsd is None:
        raise SphericalError("boîte 'stsd' introuvable")
    stsd_off, stsd_size, stsd_hdr = stsd
    # stsd body : version/flags(4) + entry_count(4), puis la 1ère entrée.
    entry_off = stsd_off + stsd_hdr + 8
    entry_typ, entry_size, entry_hdr = _read_box_header(buf, entry_off)
    if entry_typ not in _VIDEO_SAMPLE_ENTRY_TYPES:
        raise SphericalError(f"entrée d'échantillon vidéo inattendue : {entry_typ!r}")
    ancestors = [
        (mdia_off, mdia_hdr, mdia_size),
        (minf_off, minf_hdr, minf_size),
        (stbl_off, stbl_hdr, stbl_size),
        (stsd_off, stsd_hdr, stsd_size),
        (entry_off, entry_hdr, entry_size),
    ]
    return ancestors


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def inject_spherical(mp4_in: str, mp4_out: str) -> None:
    """Injecte les métadonnées sphériques V1 (uuid GSpherical) + V2 (sv3d) dans
    la piste vidéo de ``mp4_in``, en mono équirectangulaire plein cadre, et
    écrit le résultat dans ``mp4_out``."""
    with open(mp4_in, "rb") as fp:
        data = fp.read()

    out, moov_off, moov_size, moov_hdr = _relocate_moov_to_end(data)
    moov_body_start = moov_off + moov_hdr
    moov_body_end = moov_off + moov_size

    trak_off, trak_size, trak_hdr = _find_video_trak(out, moov_body_start, moov_body_end)
    ancestors = _find_sample_entry(out, trak_off, trak_size, trak_hdr)
    # ancestors = [mdia, minf, stbl, stsd, entry] ; on ajoute trak et moov, qui
    # englobent forcément aussi le point d'insertion.
    full_chain = [(trak_off, trak_hdr, trak_size), (moov_off, moov_hdr, moov_size)] + ancestors

    entry_off, entry_hdr, entry_size = ancestors[-1]
    sv3d_bytes = _build_sv3d()
    insert_pos = entry_off + entry_size
    out[insert_pos:insert_pos] = sv3d_bytes
    delta = len(sv3d_bytes)
    for box_off, box_hdr, box_size in full_chain:
        _grow_box(out, box_off, box_hdr, box_size, delta)

    # Après la croissance V2, trak s'est agrandi de `delta` : recalcule sa fin
    # pour insérer le uuid V1 comme tout dernier enfant du trak.
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
    """Écrit un GPX (side-car) contenant la trace rééchantillonnée (1 Hz) sur
    la fenêtre vidéo — utile pour Street View Studio ou vérification manuelle."""
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

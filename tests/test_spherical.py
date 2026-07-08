"""Tests de app/core/spherical.py — métadonnées sphériques V1 (uuid GSpherical)
+ V2 (sv3d/proj/equi) et export GPX fenêtré (side-car).

Vérifie que ffprobe expose bien le side data "Spherical Mapping" (V2) après
injection, que la boîte uuid V1 est présente et contient le XML attendu, que
le fichier reste décodable (vidéo, et vidéo+audio), et que l'enchaînement
spherical -> camm (ordre du pipeline réel, cf. SPEC.md) fonctionne toujours.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app.core import camm, gpx, spherical

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
EXIFTOOL = shutil.which("exiftool")

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe absents du système")

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "track_streetview.gpx")
VIDEO_START = datetime(2026, 7, 7, 20, 0, 0, tzinfo=timezone.utc)
DURATION_S = 10.0


def _make_video(path: str, with_audio: bool, faststart: bool) -> None:
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x160:rate=25:duration={DURATION_S}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={DURATION_S}"]
    cmd += ["-metadata", "creation_time=2026-07-07T20:00:00Z", "-pix_fmt", "yuv420p", "-c:v", "libx264"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += [path]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _ffprobe_json(path: str) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", path],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def _find_v1_uuid_xml(mp4_path: str) -> str:
    """Relit la boîte uuid V1 directement (indépendant de ffprobe/exiftool)."""
    data = open(mp4_path, "rb").read()

    def read_hdr(off):
        size = int.from_bytes(data[off:off + 4], "big")
        typ = data[off + 4:off + 8]
        hdr = 8
        if size == 1:
            size = int.from_bytes(data[off + 8:off + 16], "big")
            hdr = 16
        return typ, size, hdr

    def children(start, end):
        off = start
        while off + 8 <= end:
            typ, size, hdr = read_hdr(off)
            yield typ, off, size, hdr
            off += size

    off, n = 0, len(data)
    moov = None
    while off + 8 <= n:
        typ, size, hdr = read_hdr(off)
        if typ == b"moov":
            moov = (off, size, hdr)
            break
        off += size
    assert moov is not None
    moov_off, moov_size, moov_hdr = moov

    for typ, toff, tsize, thdr in children(moov_off + moov_hdr, moov_off + moov_size):
        if typ != b"trak":
            continue
        for typ2, off2, size2, hdr2 in children(toff + thdr, toff + tsize):
            if typ2 == b"uuid":
                body = off2 + hdr2
                extended_type = data[body:body + 16]
                if extended_type == spherical.V1_UUID:
                    return data[body + 16:off2 + size2].decode("utf-8")
    raise AssertionError("boîte uuid V1 introuvable dans la piste vidéo")


@pytest.mark.parametrize("with_audio,faststart", [(False, False), (False, True), (True, False)])
def test_inject_spherical_readable_by_ffprobe(tmp_path, with_audio, faststart):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=with_audio, faststart=faststart)

    spherical.inject_spherical(src, out)

    info = _ffprobe_json(out)
    assert float(info["format"]["duration"]) == pytest.approx(DURATION_S, abs=0.1)

    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    side_data = video_stream.get("side_data_list") or []
    spherical_sd = next((sd for sd in side_data if sd.get("side_data_type") == "Spherical Mapping"), None)
    assert spherical_sd is not None, f"pas de side data sphérique : {side_data}"
    assert spherical_sd["projection"] == "equirectangular"

    if with_audio:
        assert any(s["codec_type"] == "audio" for s in info["streams"])

    subprocess.run(
        [FFMPEG, "-v", "error", "-i", out, "-map", "0:v:0", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )


def test_inject_spherical_v1_uuid_xml_content(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)

    spherical.inject_spherical(src, out)
    xml = _find_v1_uuid_xml(out)

    assert "<GSpherical:Spherical>true</GSpherical:Spherical>" in xml
    assert "<GSpherical:Stitched>true</GSpherical:Stitched>" in xml
    assert "<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>" in xml


def test_inject_spherical_then_camm_pipeline_order(tmp_path):
    """Ordre réel du pipeline (SPEC.md) : inject_spherical() d'abord, puis
    resample()+inject_camm() sur le résultat. Les deux doivent survivre."""
    src = str(tmp_path / "src.mp4")
    sph = str(tmp_path / "sph.mp4")
    final = str(tmp_path / "final.mp4")
    _make_video(src, with_audio=True, faststart=False)

    spherical.inject_spherical(src, sph)

    points = gpx.parse_gpx(FIXTURE)
    samples = gpx.resample(points, VIDEO_START, DURATION_S, 0.0, rate_hz=1.0)
    camm.inject_camm(sph, final, samples, VIDEO_START)

    info = _ffprobe_json(final)
    assert float(info["format"]["duration"]) == pytest.approx(DURATION_S, abs=0.1)
    kinds = [(s["codec_type"], s.get("codec_tag_string")) for s in info["streams"]]
    assert ("data", "camm") in kinds
    assert any(k[0] == "audio" for k in kinds)

    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    side_data = video_stream.get("side_data_list") or []
    assert any(sd.get("side_data_type") == "Spherical Mapping" for sd in side_data)

    xml = _find_v1_uuid_xml(final)
    assert "equirectangular" in xml

    subprocess.run(
        [FFMPEG, "-v", "error", "-i", final, "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )


def test_export_windowed_gpx_writes_valid_windowed_track(tmp_path):
    points = gpx.parse_gpx(FIXTURE)
    out_gpx = str(tmp_path / "sidecar.gpx")
    spherical.export_windowed_gpx(points, VIDEO_START, DURATION_S, 0.0, out_gpx)

    assert os.path.isfile(out_gpx)
    reparsed = gpx.parse_gpx(out_gpx)
    assert len(reparsed) == 11
    assert reparsed[0].t == VIDEO_START
    assert reparsed[-1].t == VIDEO_START + timedelta(seconds=DURATION_S)


@pytest.mark.skipif(not EXIFTOOL, reason="exiftool absent du système")
def test_inject_spherical_readable_by_exiftool(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)

    spherical.inject_spherical(src, out)

    proc = subprocess.run(
        [EXIFTOOL, "-G3", "-j", out],
        check=True, capture_output=True, text=True,
    )
    entries = json.loads(proc.stdout)[0]
    flat = {k.split(":")[-1]: v for k, v in entries.items()}
    assert str(flat.get("Spherical")).lower() == "true"
    assert str(flat.get("Stitched")).lower() == "true"
    assert flat.get("ProjectionType") == "equirectangular"

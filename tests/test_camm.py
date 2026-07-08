"""Tests de app/core/camm.py — injection d'une piste CAMM (GPS type 6) dans un MP4.

Construit de petits MP4 de test avec ffmpeg (testsrc, silencieux ou avec piste
audio, moov avant/après mdat) et vérifie :
  - que ffprobe relit le fichier sans erreur, avec la durée intacte et une
    piste de données taguée 'camm' ;
  - que les paquets GPS injectés se décodent (avec notre propre lecteur de
    boîtes, sans dépendance externe) avec les bonnes valeurs aux bons PTS ;
  - avec exiftool si disponible, que les mêmes valeurs sont lisibles par un
    outil tiers (alignement GPX<->vidéo correct).
"""
import json
import os
import shutil
import struct
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app.core import camm, gpx

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


def _resampled_samples(offset_s: float = 0.0, rate_hz: float = 1.0):
    points = gpx.parse_gpx(FIXTURE)
    return gpx.resample(points, VIDEO_START, DURATION_S, offset_s, rate_hz)


def _decode_camm_samples(mp4_path: str):
    """Relit les échantillons CAMM injectés directement depuis les boîtes MP4
    (indépendamment de ffprobe/exiftool) : retourne une liste de dicts
    {pts_s, time_gps_epoch, lat, lon, alt}."""
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

    # trouve moov (peut être n'importe où : on ne suppose rien de l'ordre)
    moov = None
    off, n = 0, len(data)
    while off + 8 <= n:
        typ, size, hdr = read_hdr(off)
        if typ == b"moov":
            moov = (off, size, hdr)
            break
        off += size
    assert moov is not None, "moov introuvable dans le fichier produit"
    moov_off, moov_size, moov_hdr = moov

    camm_trak = None
    for typ, toff, tsize, thdr in children(moov_off + moov_hdr, moov_off + moov_size):
        if typ != b"trak":
            continue
        for typ2, off2, size2, hdr2 in children(toff + thdr, toff + tsize):
            if typ2 == b"mdia":
                for typ3, off3, size3, hdr3 in children(off2 + hdr2, off2 + size2):
                    if typ3 == b"hdlr":
                        handler = data[off3 + hdr3 + 8:off3 + hdr3 + 12]
                        if handler == b"camm":
                            camm_trak = (toff, tsize, thdr)
    assert camm_trak is not None, "piste 'camm' introuvable dans moov"
    toff, tsize, thdr = camm_trak

    mdia = next(c for c in children(toff + thdr, toff + tsize) if c[0] == b"mdia")
    minf = next(c for c in children(mdia[1] + mdia[3], mdia[1] + mdia[2]) if c[0] == b"minf")
    stbl = next(c for c in children(minf[1] + minf[3], minf[1] + minf[2]) if c[0] == b"stbl")

    stts = stco = stsz = None
    for typ, off_, size_, hdr_ in children(stbl[1] + stbl[3], stbl[1] + stbl[2]):
        if typ == b"stts":
            stts = (off_, size_, hdr_)
        elif typ in (b"stco", b"co64"):
            stco = (typ, off_, size_, hdr_)
        elif typ == b"stsz":
            stsz = (off_, size_, hdr_)

    assert stts and stco and stsz

    # stsz : sample_size constant si != 0
    body = stsz[0] + stsz[2]
    sample_size = int.from_bytes(data[body + 4:body + 8], "big")
    sample_count = int.from_bytes(data[body + 8:body + 12], "big")
    assert sample_size == 60, "paquet CAMM type 6 attendu à 60 octets"

    # stco/co64 : un seul chunk contenant tous les échantillons (cf. camm.py)
    typ, off_, size_, hdr_ = stco
    body = off_ + hdr_
    count = int.from_bytes(data[body + 4:body + 8], "big")
    assert count == 1
    if typ == b"stco":
        first_offset = int.from_bytes(data[body + 8:body + 12], "big")
    else:
        first_offset = int.from_bytes(data[body + 8:body + 16], "big")

    # stts : reconstruit les PTS cumulés
    body = stts[0] + stts[2]
    run_count = int.from_bytes(data[body + 4:body + 8], "big")
    durations = []
    p = body + 8
    for _ in range(run_count):
        cnt, dur = struct.unpack(">II", data[p:p + 8])
        durations += [dur] * cnt
        p += 8
    assert len(durations) == sample_count

    samples = []
    off = first_offset
    pts = 0
    for i in range(sample_count):
        raw = data[off:off + 60]
        reserved, ptype, epoch, fix_type, lat, lon, alt, h_acc, v_acc, vel_e, vel_n, vel_up, speed_acc = (
            struct.unpack("<HHdiddfffffff", raw)
        )
        assert reserved == 0
        assert ptype == 6
        samples.append({
            "pts_ticks": pts,
            "time_gps_epoch": epoch,
            "fix_type": fix_type,
            "lat": lat, "lon": lon, "alt": alt,
            "h_acc": h_acc, "v_acc": v_acc,
            "vel_e": vel_e, "vel_n": vel_n, "vel_up": vel_up,
            "speed_acc": speed_acc,
        })
        off += 60
        pts += durations[i]
    return samples


@pytest.mark.parametrize("with_audio,faststart", [(False, False), (False, True), (True, False)])
def test_inject_camm_readable_by_ffprobe(tmp_path, with_audio, faststart):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=with_audio, faststart=faststart)

    samples = _resampled_samples()
    camm.inject_camm(src, out, samples, VIDEO_START)

    info = _ffprobe_json(out)
    assert float(info["format"]["duration"]) == pytest.approx(DURATION_S, abs=0.1)

    kinds = [(s["codec_type"], s.get("codec_tag_string")) for s in info["streams"]]
    assert ("video", "avc1") in kinds
    assert ("data", "camm") in kinds
    if with_audio:
        assert any(k[0] == "audio" for k in kinds)

    # le fichier doit rester intégralement décodable (pas juste "ouvrable")
    subprocess.run(
        [FFMPEG, "-v", "error", "-i", out, "-map", "0:v:0", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )


def test_inject_camm_packets_match_samples_and_are_time_aligned(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)

    samples = _resampled_samples()
    camm.inject_camm(src, out, samples, VIDEO_START)

    decoded = _decode_camm_samples(out)
    assert len(decoded) == len(samples) == 11

    # PTS strictement monotone
    ticks = [d["pts_ticks"] for d in decoded]
    assert ticks == sorted(ticks)
    assert len(set(ticks)) == len(ticks)
    assert ticks[0] == 0

    # PTS (converti en secondes, timescale 90000) == décalage vidéo attendu (0..10s)
    for i, d in enumerate(decoded):
        assert d["pts_ticks"] / camm.CAMM_TIMESCALE == pytest.approx(i * 1.0, abs=1e-6)

    # valeurs GPS + alignement temporel : le paquet i doit correspondre exactement
    # à samples[i] (même lat/lon/alt, et l'epoch GPS == instant réel vidéo, cf.
    # docstring gpx.py : après correction d'offset, time_gps_epoch == heure vidéo)
    for i, (d, s) in enumerate(zip(decoded, samples)):
        assert d["lat"] == pytest.approx(s.lat, abs=1e-9)
        assert d["lon"] == pytest.approx(s.lon, abs=1e-9)
        assert d["alt"] == pytest.approx(s.ele, abs=1e-3)
        expected_epoch = s.t.timestamp()
        assert d["time_gps_epoch"] == pytest.approx(expected_epoch, abs=1e-6)
        # l'instant GPS doit tomber exactement à VIDEO_START + i secondes
        assert d["time_gps_epoch"] == pytest.approx((VIDEO_START + timedelta(seconds=i)).timestamp(), abs=1e-6)


def test_inject_camm_with_offset_keeps_video_timeline_but_shifts_position(tmp_path):
    """Le GPX est décalé de +5 s (horloge GPX en avance) : la position injectée
    doit correspondre à la trace brute 5 s plus loin, mais rester placée aux
    mêmes PTS vidéo (0..10 s) — c'est tout l'intérêt du curseur d'offset."""
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)

    points = gpx.parse_gpx(FIXTURE)
    samples_0 = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=0.0, rate_hz=1.0)
    samples_5 = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=5.0, rate_hz=1.0)

    camm.inject_camm(src, out, samples_5, VIDEO_START)
    decoded = _decode_camm_samples(out)

    # même PTS que le cas offset=0 (même nombre d'échantillons, mêmes deltas)
    ticks = [d["pts_ticks"] for d in decoded]
    assert ticks[0] == 0
    assert ticks[-1] / camm.CAMM_TIMESCALE == pytest.approx(10.0, abs=1e-6)

    # la position au 1er échantillon doit être celle du GPX brut 5 s plus tard
    raw_at_t5 = next(p for p in points if p.t == VIDEO_START + timedelta(seconds=5))
    assert decoded[0]["lat"] == pytest.approx(raw_at_t5.lat, abs=1e-9)
    assert decoded[0]["lon"] == pytest.approx(raw_at_t5.lon, abs=1e-9)
    # mais l'epoch GPS enregistré reste l'heure vidéo (VIDEO_START), pas 19:59:55+5s
    assert decoded[0]["time_gps_epoch"] == pytest.approx(VIDEO_START.timestamp(), abs=1e-6)
    assert decoded[0]["lat"] != pytest.approx(samples_0[0].lat, abs=1e-9)


def test_inject_camm_empty_samples_raises(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)
    with pytest.raises(camm.CammError):
        camm.inject_camm(src, out, [], VIDEO_START)


@pytest.mark.skipif(not EXIFTOOL, reason="exiftool absent du système")
def test_inject_camm_readable_by_exiftool(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)

    samples = _resampled_samples()
    camm.inject_camm(src, out, samples, VIDEO_START)

    proc = subprocess.run(
        [EXIFTOOL, "-ee", "-G3", "-j", out],
        check=True, capture_output=True, text=True,
    )
    entries = json.loads(proc.stdout)[0]
    gps_dates = [v for k, v in entries.items() if k.endswith(":GPSDateTime")]
    gps_lats = [v for k, v in entries.items() if k.endswith(":GPSLatitude")]
    assert len(gps_dates) == 11
    assert gps_dates[0].startswith("2026:07:07 20:00:00")
    assert gps_dates[-1].startswith("2026:07:07 20:00:10")
    assert len(gps_lats) == 11

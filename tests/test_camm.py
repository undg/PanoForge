"""Tests for app/core/camm.py — injection of a CAMM track (GPS type 6) into an MP4.

Builds small test MP4s with ffmpeg (testsrc, silent or with an audio track,
moov before/after mdat) and verifies:
  - that ffprobe reads the file back without error, with the duration intact and
    a data track tagged 'camm';
  - that the injected GPS packets decode (with our own box reader, without
    external dependency) with the right values at the right PTS;
  - with exiftool if available, that the same values are readable by a
    third-party tool (correct GPX<->video alignment).
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

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe missing from the system")

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
    """Read back the injected CAMM samples directly from the MP4 boxes
    (independently of ffprobe/exiftool): returns a list of dicts
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

    # find moov (can be anywhere: nothing is assumed about the order)
    moov = None
    off, n = 0, len(data)
    while off + 8 <= n:
        typ, size, hdr = read_hdr(off)
        if typ == b"moov":
            moov = (off, size, hdr)
            break
        off += size
    assert moov is not None, "moov not found in the produced file"
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
    assert camm_trak is not None, "track 'camm' not found in moov"
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

    # stsz: sample_size constant if != 0
    body = stsz[0] + stsz[2]
    sample_size = int.from_bytes(data[body + 4:body + 8], "big")
    sample_count = int.from_bytes(data[body + 8:body + 12], "big")
    assert sample_size == 60, "CAMM type 6 packet expected at 60 bytes"

    # stco/co64: a single chunk containing all samples (cf. camm.py)
    typ, off_, size_, hdr_ = stco
    body = off_ + hdr_
    count = int.from_bytes(data[body + 4:body + 8], "big")
    assert count == 1
    if typ == b"stco":
        first_offset = int.from_bytes(data[body + 8:body + 12], "big")
    else:
        first_offset = int.from_bytes(data[body + 8:body + 16], "big")

    # stts: reconstruct the cumulative PTS
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

    # the file must remain fully decodable (not just "openable")
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

    # strictly monotonic PTS
    ticks = [d["pts_ticks"] for d in decoded]
    assert ticks == sorted(ticks)
    assert len(set(ticks)) == len(ticks)
    assert ticks[0] == 0

    # PTS (converted to seconds, timescale 90000) == expected video offset (0..10s)
    for i, d in enumerate(decoded):
        assert d["pts_ticks"] / camm.CAMM_TIMESCALE == pytest.approx(i * 1.0, abs=1e-6)

    # GPS values + time alignment: packet i must correspond exactly
    # to samples[i] (same lat/lon/alt, and the GPS epoch == real video instant, cf.
    # gpx.py docstring: after offset correction, time_gps_epoch == video time)
    for i, (d, s) in enumerate(zip(decoded, samples)):
        assert d["lat"] == pytest.approx(s.lat, abs=1e-9)
        assert d["lon"] == pytest.approx(s.lon, abs=1e-9)
        assert d["alt"] == pytest.approx(s.ele, abs=1e-3)
        expected_epoch = s.t.timestamp()
        assert d["time_gps_epoch"] == pytest.approx(expected_epoch, abs=1e-6)
        # the GPS instant must fall exactly at VIDEO_START + i seconds
        assert d["time_gps_epoch"] == pytest.approx((VIDEO_START + timedelta(seconds=i)).timestamp(), abs=1e-6)


def test_inject_camm_with_offset_keeps_video_timeline_but_shifts_position(tmp_path):
    """The GPX is shifted by +5 s (GPX clock ahead): the injected position
    must correspond to the raw track 5 s later, but stay placed at the
    same video PTS (0..10 s) — this is the whole point of the offset slider."""
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)

    points = gpx.parse_gpx(FIXTURE)
    samples_0 = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=0.0, rate_hz=1.0)
    samples_5 = gpx.resample(points, VIDEO_START, DURATION_S, offset_s=5.0, rate_hz=1.0)

    camm.inject_camm(src, out, samples_5, VIDEO_START)
    decoded = _decode_camm_samples(out)

    # same PTS as the offset=0 case (same number of samples, same deltas)
    ticks = [d["pts_ticks"] for d in decoded]
    assert ticks[0] == 0
    assert ticks[-1] / camm.CAMM_TIMESCALE == pytest.approx(10.0, abs=1e-6)

    # the position at the 1st sample must be that of the raw GPX 5 s later
    raw_at_t5 = next(p for p in points if p.t == VIDEO_START + timedelta(seconds=5))
    assert decoded[0]["lat"] == pytest.approx(raw_at_t5.lat, abs=1e-9)
    assert decoded[0]["lon"] == pytest.approx(raw_at_t5.lon, abs=1e-9)
    # but the recorded GPS epoch remains the video time (VIDEO_START), not 19:59:55+5s
    assert decoded[0]["time_gps_epoch"] == pytest.approx(VIDEO_START.timestamp(), abs=1e-6)
    assert decoded[0]["lat"] != pytest.approx(samples_0[0].lat, abs=1e-9)


def test_inject_camm_empty_samples_raises(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "out.mp4")
    _make_video(src, with_audio=False, faststart=False)
    with pytest.raises(camm.CammError):
        camm.inject_camm(src, out, [], VIDEO_START)


@pytest.mark.skipif(not EXIFTOOL, reason="exiftool missing from the system")
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

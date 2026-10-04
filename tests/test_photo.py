"""Tests for app/core/photo.py + /api/photo/* endpoints (local copy of the OSV)."""
import json
import os
import subprocess

import pytest

from app.core import photo

import glob
from pathlib import Path

# Samples folder for local integration tests (skip if absent).
# Overridable via PANOFORGE_TEST_SAMPLES; default = ~/Vidéos/osmo360/echantillons.
SAMPLES_DIR = os.environ.get(
    "PANOFORGE_TEST_SAMPLES", str(Path.home() / "Vidéos" / "osmo360" / "echantillons")
)
_osvs = sorted(glob.glob(os.path.join(SAMPLES_DIR, "*.OSV")))
_jpgs = sorted(glob.glob(os.path.join(SAMPLES_DIR, "*.JPG"))
               + glob.glob(os.path.join(SAMPLES_DIR, "*.jpg")))
SAMPLE_OSV = _osvs[0] if _osvs else os.path.join(SAMPLES_DIR, "aucun.OSV")
SAMPLE_JPG = _jpgs[0] if _jpgs else os.path.join(SAMPLES_DIR, "aucune.JPG")

requires_sample = pytest.mark.skipif(
    not os.path.isfile(SAMPLE_OSV),
    reason="local copy of the example OSV absent",
)
requires_sample_jpg = pytest.mark.skipif(
    not os.path.isfile(SAMPLE_JPG),
    reason="example 360 photo absent",
)


def _ffprobe_dims(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,codec_name",
         "-print_format", "json", path],
        capture_output=True, text=True, timeout=30).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"], s["codec_name"]


def _make_equirect_jpg(path, w=1024, h=512):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=duration=0.1:size={w}x{h}:rate=1",
         "-frames:v", "1", "-q:v", "2", str(path)],
        capture_output=True, text=True, timeout=30, check=True)


# ---------------------------------------------------------------------------
# Units (without OSV)
# ---------------------------------------------------------------------------


def test_norm180():
    assert photo._norm180(0) == 0
    assert photo._norm180(180) == -180        # upper bound wrapped
    assert photo._norm180(-147.1 + 180) == pytest.approx(32.9)
    assert photo._norm180(280) == pytest.approx(-80)
    assert photo._norm180(-190) == pytest.approx(170)


def test_flat_angles_viewer_to_v360():
    # The heart of the bug: the viewer samples u=yaw/360, v360 aims at the center
    # -> +180° yaw offset (measured: sweep.jpg, yaw=-147.1 -> v360 32.9).
    y, p, r = photo._flat_angles_v360(-147.1, -1.3, 0.0)
    assert y == pytest.approx(32.9)
    assert p == pytest.approx(-1.3)   # pitch identical (positive = up)
    assert r == pytest.approx(0.0)
    # roll inverted (rotateZ(-roll) on the three.js side)
    _, _, r2 = photo._flat_angles_v360(0.0, 0.0, 25.0)
    assert r2 == pytest.approx(-25.0)
    # yaw always wrapped into [-180, 180]
    y2, _, _ = photo._flat_angles_v360(100.0, 0.0, 0.0)  # 100+180=280 -> -80
    assert y2 == pytest.approx(-80.0)


def _extract_vf(monkeypatch, tmp_path, projection, params):
    """Renders a reprojection by intercepting the ffmpeg command -> returns the v360 filter."""
    captured = {}
    src = tmp_path / "equirect.jpg"
    src.write_bytes(b"\xff\xd8\xff\xd9")  # file present (dims via monkeypatch)

    def fake_run(cmd, timeout=120, what="ffmpeg"):
        captured["vf"] = cmd[cmd.index("-vf") + 1]
        open(cmd[-1], "wb").write(b"\xff\xd8\xff\xd9")

    monkeypatch.setattr(photo, "_run", fake_run)
    monkeypatch.setattr(photo, "probe_dims", lambda p: (2048, 1024))
    monkeypatch.setattr(photo, "_inject_gpano", lambda *a, **k: None)
    photo.reproject(str(src), projection, params, str(tmp_path / "out.jpg"))
    return captured["vf"]


def test_reproject_flat_applies_180_offset(monkeypatch, tmp_path):
    vf = _extract_vf(monkeypatch, tmp_path, "flat",
                     {"yaw_deg": -147.1, "pitch_deg": -1.3, "roll_deg": 0,
                      "h_fov_deg": 111, "ratio": "32:9", "out_w": 1600})
    assert "output=flat" in vf
    assert "yaw=32.9" in vf          # -147.1 + 180
    assert "pitch=-1.3" in vf
    assert "roll=0" in vf


def test_reproject_flat_roll_inverted(monkeypatch, tmp_path):
    vf = _extract_vf(monkeypatch, tmp_path, "flat",
                     {"yaw_deg": 0, "pitch_deg": 0, "roll_deg": 25,
                      "h_fov_deg": 90, "ratio": "16:9", "out_w": 1280})
    assert "yaw=-180" in vf          # 0 + 180, wrapped into [-180,180)
    assert "roll=-25" in vf


def test_reproject_cylindrical_no_offset(monkeypatch, tmp_path):
    vf = _extract_vf(monkeypatch, tmp_path, "cylindrical",
                     {"yaw_deg": 90, "v_span_deg": 60, "out_w": 1600})
    assert "output=cylindrical" in vf
    assert "yaw=90" in vf            # cylindrical: no offset


def test_reproject_littleplanet_hflip_and_rotation(monkeypatch, tmp_path):
    # the planet "rotation" arrives in roll_deg (see frontend)
    vf = _extract_vf(monkeypatch, tmp_path, "littleplanet",
                     {"roll_deg": 45, "yaw_deg": 999, "out_w": 800})
    assert "output=sg" in vf
    assert "pitch=-90" in vf
    assert "yaw=135" in vf           # rotation 45 + 90
    assert "roll=0" in vf
    assert vf.rstrip().endswith("hflip") or ",hflip" in vf
    assert "h_fov=250" in vf         # fixed FOV = preview PLANET_FOV_DEG
    assert "999" not in vf           # the stray yaw_deg is ignored

def test_flat_dims_and_vfov():
    w, h, v_fov = photo._flat_dims_and_vfov(1920, "16:9", 90.0)
    assert (w, h) == (1920, 1080)
    # v_fov = 2·atan(tan(45°)·1080/1920) = 2·atan(0.5625) ≈ 58.72°
    assert v_fov == pytest.approx(58.72, abs=0.05)
    # 21:9 narrower vertically
    _, h219, v219 = photo._flat_dims_and_vfov(2100, "21:9", 90.0)
    assert h219 == 900
    assert v219 < v_fov


def test_free_ratio_decimal_valid():
    # custom decimal ratio (cinemascope 2.35:1)
    w, h, v_fov = photo._flat_dims_and_vfov(2350, "2.35:1", 90.0)
    assert (w, h) == (2350, 1000)
    assert 0 < v_fov < 90
    # "5:4" is not a preset but is a valid custom ratio
    w54, h54, _ = photo._flat_dims_and_vfov(1000, "5:4", 90.0)
    assert (w54, h54) == (1000, 800)


def test_free_ratio_out_of_bounds():
    with pytest.raises(photo.PhotoError, match="out of bounds"):
        photo._flat_dims_and_vfov(1920, "9:1", 90.0)   # 9 > 8
    with pytest.raises(photo.PhotoError, match="out of bounds"):
        photo._flat_dims_and_vfov(1920, "1:6", 90.0)   # ≈0.167 < 0.2


def test_free_ratio_malformed():
    for bad in ("abc", "16/9", "16:9:2", ":", "16:", "-16:9", "0:1", "nan:1"):
        with pytest.raises(photo.PhotoError, match="ratio"):
            photo._flat_dims_and_vfov(1920, bad, 90.0)


def test_flat_hfov_clamped():
    _, _, v_lo = photo._flat_dims_and_vfov(1920, "16:9", 10.0)   # clamped to 30
    _, _, v30 = photo._flat_dims_and_vfov(1920, "16:9", 30.0)
    assert v_lo == pytest.approx(v30)


def test_source_kind_and_errors(tmp_path):
    assert photo._source_kind("a.OSV") == "osv"
    assert photo._source_kind("a.Mp4") == "mp4"
    assert photo._source_kind("a.JPEG") == "jpg"
    with pytest.raises(photo.PhotoError):
        photo._source_kind("a.txt")
    with pytest.raises(photo.PhotoError):
        photo.get_equirect_frame(str(tmp_path / "missing.mp4"), 0.0, str(tmp_path))


def test_jpeg_non_equirect_rejected(tmp_path):
    bad = tmp_path / "photo_4x3.jpg"
    _make_equirect_jpg(bad, 800, 600)  # ratio 4:3, not 2:1
    with pytest.raises(photo.PhotoError, match="equirectangular"):
        photo.get_equirect_frame(str(bad), 0.0, str(tmp_path))


def test_jpeg_equirect_used_as_is(tmp_path):
    good = tmp_path / "pano.jpg"
    _make_equirect_jpg(good, 1024, 512)
    result = photo.get_equirect_frame(str(good), 0.0, str(tmp_path))
    assert result == str(good)


def test_reproject_all_projections_from_synthetic(tmp_path):
    src = tmp_path / "equirect.jpg"
    _make_equirect_jpg(src, 1024, 512)

    w, h = photo.reproject(str(src), "flat",
                           {"out_w": 640, "ratio": "16:9", "h_fov_deg": 90},
                           str(tmp_path / "flat.jpg"))
    assert (w, h) == (640, 360)
    assert _ffprobe_dims(str(tmp_path / "flat.jpg"))[:2] == (640, 360)

    w, h = photo.reproject(str(src), "cylindrical",
                           {"out_w": 720, "v_span_deg": 60},
                           str(tmp_path / "cyl.jpg"))
    assert w == 720 and 0 < h < 360  # full turn, thin strip

    w, h = photo.reproject(str(src), "littleplanet", {"out_w": 400},
                           str(tmp_path / "lp.jpg"))
    assert (w, h) == (400, 400)

    with pytest.raises(photo.PhotoError, match="unknown projection"):
        photo.reproject(str(src), "cube", {}, str(tmp_path / "x.jpg"))


def test_equirect360_gpano_injected(tmp_path):
    src = tmp_path / "equirect.jpg"
    _make_equirect_jpg(src, 1024, 512)
    out = tmp_path / "pano360.jpg"
    w, h = photo.reproject(str(src), "equirect360", {"out_w": 1024}, str(out))
    assert (w, h) == (1024, 512)

    data = out.read_bytes()
    assert data[:2] == b"\xff\xd8"
    assert photo.XMP_MARKER in data
    assert b'GPano:ProjectionType="equirectangular"' in data
    assert b'GPano:FullPanoWidthPixels="1024"' in data
    # the JPEG remains decodable after injection
    assert _ffprobe_dims(str(out))[:2] == (1024, 512)


# ---------------------------------------------------------------------------
# OSV integration (local copy) + API
# ---------------------------------------------------------------------------

@requires_sample
def test_get_equirect_frame_from_osv(tmp_path, isolated_config):
    # low resolution for a fast test; the maps cache is isolated
    png = photo.get_equirect_frame(SAMPLE_OSV, 1.0, str(tmp_path), out_w=1536)
    w, h, codec = _ffprobe_dims(png)
    assert (w, h) == (1536, 768)
    assert codec == "png"


@requires_sample_jpg
def test_api_photo_extract_from_camera_jpg(client, isolated_config):
    """Real 360 photo from the camera (equirect 2:1, e.g. 15520x7760): flat + custom ratio."""
    r = client.post("/api/photo/extract", json={
        "source_path": SAMPLE_JPG, "projection": "flat",
        "h_fov_deg": 90, "ratio": "2.35:1", "out_w": 1880,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["width"], body["height"]) == (1880, 800)
    assert os.path.isfile(body["photo_path"])
    assert _ffprobe_dims(body["photo_path"])[:2] == (1880, 800)


@requires_sample
def test_api_photo_extract_flat(client, isolated_config):
    r = client.post("/api/photo/extract", json={
        "source_path": SAMPLE_OSV, "time_s": 1.0, "projection": "flat",
        "yaw_deg": 0, "pitch_deg": 0, "h_fov_deg": 100, "ratio": "21:9",
        "out_w": 1260,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["width"], body["height"]) == (1260, 540)
    assert os.path.isfile(body["photo_path"])
    assert "/photos/" in body["photo_path"]
    assert body["photo_path"].endswith("_1.00s_flat.jpg")
    # served by /api/media
    rr = client.get(body["preview_url"])
    assert rr.status_code == 200
    assert rr.content[:2] == b"\xff\xd8"


@requires_sample
def test_api_photo_extract_equirect360_has_gpano(client, isolated_config):
    r = client.post("/api/photo/extract", json={
        "source_path": SAMPLE_OSV, "time_s": 0.5,
        "projection": "equirect360", "out_w": 1024,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["width"], body["height"]) == (1024, 512)
    data = open(body["photo_path"], "rb").read()
    assert photo.XMP_MARKER in data and b"GPano:UsePanoramaViewer" in data


@requires_sample
def test_api_photo_extract_bad_inputs(client, isolated_config, tmp_path):
    # outside navigation scope -> 403
    r = client.post("/api/photo/extract", json={
        "source_path": "/etc/passwd", "projection": "flat"})
    assert r.status_code == 403
    # unknown projection -> explicit 400
    r2 = client.post("/api/photo/extract", json={
        "source_path": SAMPLE_OSV, "projection": "fisheye", "out_w": 512})
    assert r2.status_code == 400
    assert "unknown projection" in r2.json()["detail"]
    # malformed ratio -> immediate 400 (before stitching)
    r2b = client.post("/api/photo/extract", json={
        "source_path": SAMPLE_OSV, "projection": "flat", "ratio": "16/9"})
    assert r2b.status_code == 400
    assert "ratio" in r2b.json()["detail"]
    # time outside duration -> explicit 400
    r3 = client.post("/api/photo/extract", json={
        "source_path": SAMPLE_OSV, "time_s": 99.0, "projection": "flat",
        "out_w": 512})
    assert r3.status_code == 400


@requires_sample
def test_api_navproxy_osv(client, isolated_config):
    r = client.get("/api/photo/navproxy", params={"path": SAMPLE_OSV})
    assert r.status_code == 200, r.text
    url = r.json()["proxy_url"]
    assert url.startswith("/api/media?path=")
    rr = client.get(url, headers={"Range": "bytes=0-255"})
    assert rr.status_code == 206
    # h264 proxy 688x344
    from urllib.parse import parse_qs, urlparse
    proxy_path = parse_qs(urlparse(url).query)["path"][0]
    w, h, codec = _ffprobe_dims(proxy_path)
    assert (w, h, codec) == (688, 344, "h264")
    # 2nd call: cache (same file)
    r2 = client.get("/api/photo/navproxy", params={"path": SAMPLE_OSV})
    assert r2.json()["proxy_url"] == url


def test_api_navproxy_rejects_other_types(client, isolated_config, tmp_path):
    bad = tmp_path / "x.gpx"
    bad.write_text("<gpx/>")
    r = client.get("/api/photo/navproxy", params={"path": str(bad)})
    assert r.status_code in (400, 403)  # 403 if tmp outside HOME, otherwise 400

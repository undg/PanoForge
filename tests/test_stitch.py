"""Tests for the ffmpeg command builder (app/core/stitch.py)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.maps import MapSet  # noqa: E402
from app.core.stitch import StitchOptions, build_command  # noqa: E402

IN, OUT = "/tmp/in.OSV", "/tmp/out.mp4"


def fake_mapset(w=3840, h=1920, calibrated=True):
    return MapSet(out_w=w, out_h=h,
                  xmaps=["/tmp/x0.pgm", "/tmp/x1.pgm"],
                  ymaps=["/tmp/y0.pgm", "/tmp/y1.pgm"],
                  blend_mask="/tmp/mask.pgm", calibrated=calibrated)


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_v360_basic():
    opts = StitchOptions(out_w=7680, codec="hevc", encoder="cpu", mode="v360")
    cmd = build_command(IN, OUT, opts)
    assert cmd[0] == "ffmpeg" and cmd[-1] == OUT
    assert cmd[cmd.index("-i") + 1] == IN
    g = graph_of(cmd)
    assert "hstack" in g and "v360=input=dfisheye:output=e" in g
    assert "ih_fov=190" in g and "yaw=90" in g
    assert "w=7680:h=3840" in g and "interp=lanczos" in g
    assert "-progress" in cmd and cmd[cmd.index("-progress") + 1] == "pipe:1"
    assert "-nostats" in cmd
    assert "libx265" in cmd and "hvc1" in cmd
    # audio copied
    assert "0:a?" in cmd and "copy" in cmd


def test_v360_h264_line_fps():
    opts = StitchOptions(out_w=3840, codec="h264", encoder="cpu",
                         interp="line", fps_out=5, quality=23)
    opts.mode = "v360"
    cmd = build_command(IN, OUT, opts)
    g = graph_of(cmd)
    assert "fps=5," in g and "interp=line" in g and "w=3840:h=1920" in g
    assert "libx264" in cmd
    assert cmd[cmd.index("-crf") + 1] == "23"


def test_calibrated_graph():
    ms = fake_mapset()
    opts = StitchOptions(out_w=3840, encoder="cpu", mode="calibrated")
    cmd = build_command(IN, OUT, opts, ms)
    g = graph_of(cmd)
    assert g.count("remap") == 2 and "maskedmerge" in g
    for p in ms.xmaps + ms.ymaps + [ms.blend_mask]:
        assert p in cmd
    assert "-progress" in cmd and "-nostats" in cmd


def test_mode_auto():
    opts = StitchOptions(out_w=3840, encoder="cpu", mode="auto")
    # without maps -> v360
    assert "v360=" in graph_of(build_command(IN, OUT, opts, None))
    # calibrated maps -> calibrated
    assert "maskedmerge" in graph_of(build_command(IN, OUT, opts, fake_mapset()))
    # fallback maps (ideal geometry) -> v360 preferred
    ms = fake_mapset(calibrated=False)
    assert "v360=" in graph_of(build_command(IN, OUT, opts, ms))


def test_calibrated_requires_maps_and_matching_size():
    opts = StitchOptions(out_w=3840, encoder="cpu", mode="calibrated")
    with pytest.raises(ValueError):
        build_command(IN, OUT, opts, None)
    with pytest.raises(ValueError):
        build_command(IN, OUT, opts, fake_mapset(w=7680, h=3840))


def test_nvenc_args():
    opts = StitchOptions(out_w=3840, codec="hevc", encoder="nvenc",
                         mode="v360", quality=20)
    cmd = build_command(IN, OUT, opts)
    assert "hevc_nvenc" in cmd
    assert cmd[cmd.index("-cq") + 1] == "20"

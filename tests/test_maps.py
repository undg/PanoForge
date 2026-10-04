"""Tests for remap map generation (app/core/maps.py)."""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.maps import MapSet, generate_remap_maps  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture(scope="module")
def calibration():
    with open(os.path.join(FIXTURES, "calibration.json")) as f:
        return json.load(f)


def read_pgm(path):
    with open(path, "rb") as f:
        assert f.read(3) == b"P5\n"
        dims = f.readline().split()
        w, h = int(dims[0]), int(dims[1])
        maxval = int(f.readline())
        dt = np.dtype(">u2") if maxval > 255 else np.dtype("u1")
        data = np.frombuffer(f.read(), dt).reshape(h, w)
    return data, maxval


def test_generate_calibrated(tmp_path, calibration):
    ms = generate_remap_maps(calibration, 688, 344, str(tmp_path))
    assert isinstance(ms, MapSet)
    assert ms.calibrated is True
    assert ms.out_w == 688 and ms.out_h == 344
    assert len(ms.xmaps) == 2 and len(ms.ymaps) == 2
    for path in ms.xmaps + ms.ymaps + [ms.blend_mask]:
        assert os.path.isfile(path), path

    for path in ms.xmaps + ms.ymaps:
        data, maxval = read_pgm(path)
        assert data.shape == (344, 688)
        assert maxval == 65535
        valid = data != 65535
        # majority of valid pixels, and coordinates within the 3840x3840 source
        assert valid.mean() > 0.5
        assert data[valid].max() < 3840

    mask, maxval = read_pgm(ms.blend_mask)
    assert mask.shape == (344, 688) and maxval == 255
    # the mask contains both extremes (single-lens zones)
    assert (mask == 0).any() and (mask == 255).any()
    # and intermediate values (blending gradient)
    assert ((mask > 20) & (mask < 235)).any()


def test_front_lens_center(tmp_path, calibration):
    """The front lens direction (lenses[1]) must land near (cx,cy)."""
    ms = generate_remap_maps(calibration, 688, 344, str(tmp_path))
    x1, _ = read_pgm(ms.xmaps[1])
    y1, _ = read_pgm(ms.ymaps[1])
    # front = longitude -90° in the output (yaw offset +90°) -> column W/4
    col, row = 688 // 4, 344 // 2
    lens = calibration["lenses"][1]
    assert abs(int(x1[row, col]) - lens["cx"]) < 40
    assert abs(int(y1[row, col]) - lens["cy"]) < 40


def test_generate_fallback_none(tmp_path):
    ms = generate_remap_maps(None, 344, 172, str(tmp_path))
    assert ms.calibrated is False
    for path in ms.xmaps + ms.ymaps + [ms.blend_mask]:
        assert os.path.isfile(path)
    data, _ = read_pgm(ms.xmaps[0])
    assert data.shape == (172, 344)
    valid = data != 65535
    assert valid.mean() > 0.5

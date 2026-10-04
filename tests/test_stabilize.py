"""Tests for gyroscopic stabilization (app/core/stabilize.py).

Covers: slerp, v360 euler decoding (round trip), PTS alignment of
load_orientations, the three modes (horizon/lock/smooth), the normalized strength
[0,1], the absence of IMU (flag + zero corrections) and the construction of the
sendcmd file. An end-to-end test verifies the injection into stitch.py.
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import stabilize as S  # noqa: E402
from app.core.stitch import StitchOptions, build_command  # noqa: E402
from app.core.maps import MapSet  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PERFRAME = os.path.join(FIXTURES, "imu_perframe.csv")


# --- helpers ----------------------------------------------------------------

def _write_highrate(path, quats, fps=25.0, sub_per_frame=4):
    """Writes a synthetic imu_highrate.csv (frame,subidx,qw..qz)."""
    with open(path, "w") as fp:
        fp.write("frame,subidx,qw,qx,qy,qz\n")
        for f, q in enumerate(quats):
            for s in range(sub_per_frame):
                fp.write(f"{f},{s},{q[0]},{q[1]},{q[2]},{q[3]}\n")


def _axis_quat(axis, deg):
    a = math.radians(deg) / 2.0
    s = math.sin(a)
    ax = np.array(axis, float)
    ax = ax / np.linalg.norm(ax)
    return [math.cos(a), s * ax[0], s * ax[1], s * ax[2]]


# --- slerp ------------------------------------------------------------------

def test_slerp_endpoints_and_midpoint():
    q0 = [1.0, 0.0, 0.0, 0.0]
    q1 = _axis_quat([0, 0, 1], 90)
    assert S.slerp(q0, q1, 0.0) == pytest.approx(q0, abs=1e-9)
    assert S.slerp(q0, q1, 1.0) == pytest.approx(q1, abs=1e-9)
    mid = S.slerp(q0, q1, 0.5)
    expected = _axis_quat([0, 0, 1], 45)
    assert mid == pytest.approx(expected, abs=1e-6)


def test_slerp_shortest_path_double_cover():
    # q and -q represent the same orientation: slerp takes the shortest path.
    q0 = [1.0, 0.0, 0.0, 0.0]
    q1 = [-1.0, 0.0, 0.0, 0.0]
    mid = S.slerp(q0, q1, 0.5)
    # must stay close to identity (not a 180° rotation)
    assert abs(abs(mid[0]) - 1.0) < 1e-6


# --- v360 euler decoding (round trip) ---------------------------------------

def test_v360_euler_roundtrip():
    rng = np.random.default_rng(0)
    for _ in range(200):
        yaw = float(rng.uniform(-179, 179))
        pitch = float(rng.uniform(-89, 89))   # avoids exact gimbal lock
        roll = float(rng.uniform(-179, 179))
        V = S._vmat(yaw, pitch, roll)
        y2, p2, r2 = S._v360_euler_from_matrix(V)
        V2 = S._vmat(y2, p2, r2)
        assert np.abs(V2 - V).max() < 1e-6


def test_v360_gimbal_does_not_crash():
    V = S._vmat(30.0, 90.0, 0.0)  # pitch ±90 -> gimbal
    y, p, r = S._v360_euler_from_matrix(V)
    assert np.abs(S._vmat(y, p, r) - V).max() < 1e-6


# --- load_orientations ------------------------------------------------------

def test_load_orientations_perframe_count_and_pts(tmp_path):
    quats = S.load_orientations(PERFRAME, 25.0, 40)
    assert len(quats) == 40
    for q in quats:
        assert abs(math.sqrt(sum(c * c for c in q)) - 1.0) < 1e-6


def test_load_orientations_resamples_highrate(tmp_path):
    # roll ramp from 0 to 40° over 40 frames -> the orientation at the midpoint
    # must be ~20° (continuous slerp).
    quats = [_axis_quat([0, 0, 1], 40.0 * i / 39.0) for i in range(40)]
    hp = tmp_path / "hr.csv"
    _write_highrate(str(hp), quats, fps=25.0)
    out = S.load_orientations(str(hp), 25.0, 40)
    assert len(out) == 40
    mid = out[20]
    ang = math.degrees(2 * math.acos(min(1.0, abs(mid[0]))))
    assert 18.0 < ang < 22.0


def test_load_orientations_absent_file():
    assert S.load_orientations("/nonexistent/imu.csv", 25.0, 10) == []


# --- modes ------------------------------------------------------------------

def _flat_quats(n, tilt_deg=25.0, axis=(1, 0, 0)):
    """n frames all tilted by tilt_deg (constant orientation)."""
    return [_axis_quat(axis, tilt_deg) for _ in range(n)]


def test_horizon_levels_constant_tilt():
    # constant tilted orientation -> constant non-zero correction which, re-
    # applied, brings gravity back to the nadir (-Ye) in equirect.
    quats = _flat_quats(10, tilt_deg=30.0, axis=(1, 0, 0))
    corr = S.compute_corrections(quats, "horizon", {"strength": 1.0, "fps": 25.0})
    assert len(corr) == 10
    # identical correction on all frames (frozen orientation)
    for c in corr[1:]:
        assert c == pytest.approx(corr[0], abs=1e-6)
    # verify leveling: gravity in equirect brought back to (0,-1,0)
    M = np.array(S._BODY_TO_EQUIRECT)
    Rbw = S._quat_to_matrix(quats[0])
    g_e = M @ (Rbw.T @ np.array(S.WORLD_DOWN))
    Rc = S._vmat(*corr[0])          # v360 matrix corresponding to the correction
    leveled = Rc @ g_e
    assert leveled == pytest.approx([0.0, -1.0, 0.0], abs=1e-4)


def test_horizon_identity_when_upright():
    # "Upright" for this camera = body X pointing down (gravity),
    # i.e. Rbw sending X_body onto Z_world(down): rotation -90° around Y.
    q_upright = _axis_quat([0, 1, 0], -90.0)
    corr = S.compute_corrections([q_upright] * 5, "horizon",
                                 {"strength": 1.0, "fps": 25.0})
    # gravity already at the nadir -> nearly zero correction
    for c in corr:
        assert max(abs(v) for v in c) < 1e-3


def test_strength_scales_horizon_correction():
    quats = _flat_quats(6, tilt_deg=30.0, axis=(1, 0, 0))
    full = S.compute_corrections(quats, "horizon", {"strength": 1.0, "fps": 25.0})[0]
    half = S.compute_corrections(quats, "horizon", {"strength": 0.5, "fps": 25.0})[0]
    zero = S.compute_corrections(quats, "horizon", {"strength": 0.0, "fps": 25.0})[0]
    # strength 0 = no correction; strength 1 > strength 0.5 > strength 0 (in amplitude)
    amp = lambda c: math.sqrt(sum(v * v for v in c))
    assert amp(zero) < 1e-6
    assert amp(half) < amp(full)
    assert amp(half) > amp(zero)


def test_lock_reference_frame_is_identity():
    # reference frame (ref_index) -> identity correction.
    quats = [_axis_quat([0, 0, 1], d) for d in (0, 10, 20, 30)]
    corr = S.compute_corrections(quats, "lock",
                                 {"strength": 1.0, "fps": 25.0, "ref_index": 0})
    assert max(abs(v) for v in corr[0]) < 1e-4
    # the following frames receive a non-zero correction
    assert max(abs(v) for v in corr[3]) > 1.0


def test_smooth_returns_one_per_frame_and_reduces_jitter():
    # slow orientation + high-frequency shake: smooth must produce one
    # correction per frame and not blow up.
    base = [_axis_quat([0, 0, 1], 5.0 * math.sin(i / 5.0)) for i in range(30)]
    jitter = []
    for i, q in enumerate(base):
        j = _axis_quat([0, 0, 1], 3.0 * ((-1) ** i))
        jitter.append(S._quat_mul(q, j))
    corr = S.compute_corrections(jitter, "smooth", {"strength": 1.0, "fps": 25.0})
    assert len(corr) == 30
    for c in corr:
        assert all(math.isfinite(v) for v in c)


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        S.compute_corrections([[1, 0, 0, 0]], "nope", {})


# --- absence of IMU ---------------------------------------------------------

def test_frame_corrections_no_imu_flag():
    res = S.frame_corrections("/nonexistent.csv", 25.0, 50, "horizon", 1.0)
    assert res.has_imu is False
    assert res.corrections == []
    assert res.n_frames == 50


def test_compute_corrections_empty_quats():
    assert S.compute_corrections([], "horizon", {}) == []


# --- sendcmd construction ---------------------------------------------------

def test_build_sendcmd_format(tmp_path):
    corr = [(10.0, -5.0, 3.0), (11.0, -6.0, 4.0), (12.0, -7.0, 5.0)]
    p = tmp_path / "s.cmd"
    S.build_sendcmd(corr, 25.0, str(p))
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 3
    # triggered half a frame early (timing robustness); frame 0 -> t=0
    assert lines[0].startswith("0.000000 ")
    assert "v360 yaw" in lines[0] and "v360 pitch" in lines[0] and "v360 roll" in lines[0]
    assert lines[0].rstrip().endswith(";")
    # increasing times
    t = [float(ln.split(" ", 1)[0]) for ln in lines]
    assert t == sorted(t)


def test_build_sendcmd_empty(tmp_path):
    p = tmp_path / "e.cmd"
    S.build_sendcmd([], 25.0, str(p))
    assert p.read_text() == ""


# --- stitch.py integration --------------------------------------------------

def _fake_mapset(w=3840, h=1920):
    return MapSet(out_w=w, out_h=h, xmaps=["/tmp/x0.pgm", "/tmp/x1.pgm"],
                  ymaps=["/tmp/y0.pgm", "/tmp/y1.pgm"],
                  blend_mask="/tmp/mask.pgm", calibrated=True)


def _graph(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_stitch_v360_stabilized_neutral_init(tmp_path):
    scmd = str(tmp_path / "st.cmd")
    open(scmd, "w").close()
    opts = StitchOptions(out_w=3840, encoder="cpu", mode="v360", stabilize=True)
    g = _graph(build_command("/in.OSV", "/out.mp4", opts, stabilize_cmd=scmd))
    # a SINGLE v360, neutral init, driven by sendcmd
    assert g.count("v360=") == 1
    assert "yaw=0:pitch=0:roll=0" in g and "yaw=90" not in g
    assert "sendcmd=f=" in g and "input=dfisheye" in g


def test_stitch_calibrated_stabilized_adds_single_v360(tmp_path):
    scmd = str(tmp_path / "st.cmd")
    open(scmd, "w").close()
    opts = StitchOptions(out_w=3840, encoder="cpu", mode="calibrated", stabilize=True)
    g = _graph(build_command("/in.OSV", "/out.mp4", opts, _fake_mapset(), stabilize_cmd=scmd))
    assert "maskedmerge" in g
    assert g.count("v360=e:e") == 1
    assert "yaw=0:pitch=0:roll=0" in g and "sendcmd=f=" in g


def test_stitch_no_regression_without_stabilize():
    # without stabilize_cmd, the baseline is intact (yaw=90, no sendcmd).
    opts = StitchOptions(out_w=7680, encoder="cpu", mode="v360")
    g = _graph(build_command("/in.OSV", "/out.mp4", opts))
    assert "yaw=90" in g and "sendcmd" not in g and "v360=input=dfisheye" in g
    opts2 = StitchOptions(out_w=3840, encoder="cpu", mode="calibrated")
    g2 = _graph(build_command("/in.OSV", "/out.mp4", opts2, _fake_mapset()))
    assert "sendcmd" not in g2 and g2.count("v360") == 0


def test_stitch_path_escaping(tmp_path):
    # a path containing a comma must be escaped in filter_complex.
    scmd = str(tmp_path / "a,b.cmd")
    open(scmd, "w").close()
    opts = StitchOptions(out_w=3840, encoder="cpu", mode="v360", stabilize=True)
    g = _graph(build_command("/in.OSV", "/out.mp4", opts, stabilize_cmd=scmd))
    assert "a\\,b.cmd" in g

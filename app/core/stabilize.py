"""Gyroscopic stabilization of DJI Osmo 360 .OSV files (phase 2 — activation).

Goal: level the horizon and smooth shake by counter-rotating each
frame of the equirectangular output according to the embedded IMU orientation
(~1 kHz quaternions extracted by ``osv_meta/extract_djmd.py``).

================================================================================
AXIS CONVENTION — EMPIRICALLY VALIDATED (real handheld clip, 6.3 s:
CAM_20260708072843_0001_D.OSV) — see work/engine/stab/ for the evidence.
================================================================================

1. SOURCE QUATERNION (imu_perframe / imu_highrate): order ``[w, x, y, z]``,
   normalized (||q|| = 1.0000). It represents the BODY -> WORLD orientation:
   ``v_world = R(q) · v_body``.

   Evidence: the accelerometer (field ``ax_g,ay_g,az_g``, in g, measures the
   reaction to gravity => points toward local UP in the body frame). Testing
   the 4 combinations {order wxyz|xyzw} × {R|Rᵀ}, only ``[w,x,y,z]`` with R
   gives a vector ``R(q)·body_accel`` CONSTANT over time (mean dispersion
   ~11°, the residual being the real dynamic acceleration of the clip which
   moves by 144°); the other conventions give 29–44° of dispersion. This
   constant vector is ≈ [0, 0, -0.95]: the WORLD vertical axis is -Z
   ("down" gravity = +Z world). Cross-checked frame by frame: the gravity deduced from the
   quaternion matches the accelerometer to within 1–8° on slow frames.

2. DJI WORLD FRAME: Z points DOWN (direction of gravity), XY plane
   horizontal. (``WORLD_DOWN = [0, 0, 1]``.)

3. Output EQUIRECT FRAME (= v360 filter convention, output=e):
   Xe = right, Ye = UP (zenith at the top of the image), Ze = FORWARD (center of
   the image). yaw rotates around Ye, pitch around Xe, roll around Ze.

4. BODY -> EQUIRECT MATRIX (``BODY_TO_EQUIRECT`` = M), as produced by
   the baseline stitch ``v360=input=dfisheye:...:yaw=90``:
       Y_body (forward)  -> Ze  (center of the image)
       X_body            -> -Ye (nadir / down)
       Z_body            -> -Xe
   Verified: on the most tilted frame (135), the gravity projected in equirect
   gives a predicted roll ≈ +35° which, applied, puts the person upright
   and the horizon flat (renders base_f135.jpg vs corr_f135A.jpg). On frame 60
   (camera pointed at the sky, correction ~114°) the same chain straightens the
   scene (c60_c2.jpg).

5. v360 FILTER ROTATION CONVENTION (rorder="ypr", validated at render on
   corrections up to ~114° — frames 135 and 60): the filter applies
   to the directions the matrix
       Vmat(yaw, pitch, roll) = Ry(yaw) · Rx(pitch) · Rz(roll)
   (yaw around Ye, pitch around Xe, roll around Ze; "outer" yaw,
   consistent with the ypr order). To realize a content rotation ``Rc`` one
   solves ``Vmat(yaw,pitch,roll) = Rc`` (closed-form, exact decoding,
   ``_v360_euler_from_matrix``). This convention correctly reproduces
   LARGE multi-axis corrections, where an inverse-order decomposition
   failed.

   IMPORTANT (sendcmd mechanics): the yaw/pitch/roll commands of the v360 filter
   COMPOSE with the filter's INITIALIZATION orientation. The v360 driven
   by sendcmd MUST therefore be initialized at NEUTRAL rotation (yaw=0:pitch=0:
   roll=0); the baseline yaw=90 alignment is then folded into the angles
   sent (see ``fold_baseline_yaw``). Without this, the render is wrong.

--------------------------------------------------------------------------------
Application in the ffmpeg chain (see stitch.py) — a SINGLE v360 filter per
graph so that ``sendcmd`` targets unambiguously (targeting by filter type
is ambiguous when two v360 coexist):
  * v360 mode: the stabilization rotation is FOLDED into the stitch's v360
    (dfisheye->e). We send via sendcmd, frame by frame, the eulers of
    ``Rc · Vmat(90,0,0)`` (Vmat(90,0,0) = the baseline yaw=90 alignment). Also avoids
    a second resampling.
  * calibrated mode: the stitch uses ``remap`` (no v360); we add a
    dedicated, unique v360=e:e, driven by sendcmd with the eulers of ``Rc``.
--------------------------------------------------------------------------------
No IMU: ``frame_corrections`` returns ``([], has_imu=False)`` and the
pipeline inserts no stabilization (zero corrections).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a declared dependency
    np = None  # type: ignore


# --- Validated conventions (see header) --------------------------------------

WORLD_DOWN = (0.0, 0.0, 1.0)          # "down" gravity in the DJI world frame
# Body -> equirect (rows = Xe, Ye, Ze components; columns = body X,Y,Z)
_BODY_TO_EQUIRECT = (
    (0.0, 0.0, -1.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)
# yaw of the v360 baseline (aligns dfisheye + camera thumbnail) folded in v360 mode.
BASELINE_YAW_DEG = 90.0

VALID_MODES = ("horizon", "lock", "smooth")


@dataclass
class StabilizationResult:
    """Result ready to inject into ffmpeg."""
    corrections: list           # list of (yaw, pitch, roll) in degrees (v360 frame)
    has_imu: bool
    mode: str
    n_frames: int


# --- Small quaternion / matrix algebra (numpy if available) ------------------

def _as_np():
    if np is None:  # pragma: no cover
        raise RuntimeError("numpy required for stabilization")
    return np


def _quat_to_matrix(q):
    """q = [w, x, y, z] (unit) -> 3x3 matrix R such that v_world = R·v_body."""
    w, x, y, z = q
    return _as_np().array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])


def _quat_normalize(q):
    n = math.sqrt(sum(c * c for c in q))
    if n < 1e-12:
        return [1.0, 0.0, 0.0, 0.0]
    return [c / n for c in q]


def _quat_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def slerp(q0, q1, t):
    """Spherical interpolation between two quaternions [w,x,y,z]."""
    q0 = _quat_normalize(list(q0))
    q1 = _quat_normalize(list(q1))
    d = _quat_dot(q0, q1)
    if d < 0.0:  # shortest path (double cover)
        q1 = [-c for c in q1]
        d = -d
    if d > 0.9995:  # nearly collinear -> linear interpolation
        r = [q0[i] + t * (q1[i] - q0[i]) for i in range(4)]
        return _quat_normalize(r)
    theta0 = math.acos(max(-1.0, min(1.0, d)))
    theta = theta0 * t
    s0 = math.sin(theta0 - theta) / math.sin(theta0)
    s1 = math.sin(theta) / math.sin(theta0)
    return [s0 * q0[i] + s1 * q1[i] for i in range(4)]


def _quat_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return [
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ]


def _quat_conj(q):
    return [q[0], -q[1], -q[2], -q[3]]


# --- IMU CSV reading ---------------------------------------------------------

def parse_imu_csv(path):
    """Reads an IMU CSV (imu_highrate.csv OR imu_perframe.csv).

    Returns ``(times_s, quats)``: ``times_s`` list of relative times (s,
    origin = first row), ``quats`` list of [w,x,y,z]. ``fps`` is used to
    timestamp the high-rate stream (which has no absolute timestamp). Returns
    ``([], [])`` if no usable data.
    """
    rows = []
    try:
        with open(path, newline="") as fp:
            reader = csv.DictReader(fp)
            cols = reader.fieldnames or []
            for r in reader:
                rows.append(r)
    except (OSError, csv.Error):
        return [], []
    if not rows:
        return [], []
    has_ts = "timestamp" in cols
    has_sub = "subidx" in cols

    quats = []
    raw_ts = []
    frames = []
    subs = []
    for r in rows:
        try:
            q = [float(r["qw"]), float(r["qx"]), float(r["qy"]), float(r["qz"])]
        except (KeyError, TypeError, ValueError):
            continue
        if any(c != c for c in q):  # NaN
            continue
        quats.append(_quat_normalize(q))
        if has_ts:
            try:
                raw_ts.append(float(r["timestamp"]))
            except (TypeError, ValueError):
                raw_ts.append(None)
        if has_sub:
            try:
                frames.append(int(float(r["frame"])))
                subs.append(int(float(r["subidx"])))
            except (KeyError, TypeError, ValueError):
                frames.append(0)
                subs.append(0)
    if not quats:
        return [], []

    # Building the relative time scale (seconds).
    times = None
    if has_ts and all(t is not None for t in raw_ts) and len(raw_ts) == len(quats):
        t0 = raw_ts[0]
        # timestamps in microseconds (delta ~40002 µs @ 25 fps).
        times = [(t - t0) * 1e-6 for t in raw_ts]
        if times[-1] <= 0:  # unexpected unit -> index fallback
            times = None
    if times is None and has_sub and frames:
        # high-rate stream: timestamp by (frame + subidx/n_sub_frame)/fps.
        per_frame = {}
        for f in frames:
            per_frame[f] = per_frame.get(f, 0) + 1
        # fps set to 1.0 here; properly re-timestamped in load_orientations which
        # knows the real rate -> we return a time "in frames".
        times = []
        seen = {}
        for f, s in zip(frames, subs):
            cnt = max(1, per_frame.get(f, 1))
            times.append(f + s / cnt)  # unit = source frames
    if times is None:
        times = list(range(len(quats)))  # unit = source frames
    return times, quats


def load_orientations(imu_csv, fps, n_frames, time_base=None):
    """One orientation (quaternion [w,x,y,z]) per output frame.

    Resamples the IMU stream (slerp) at the output frame instants
    ``t_i = i / fps`` (i = 0..n_frames-1). ``time_base``: if provided, scale
    (seconds/unit) applied to source times; otherwise auto (CSVs in
    microseconds are already converted, the high-rate stream is in frames and
    converted via ``fps``). Returns ``[]`` if the IMU is absent.
    """
    times, quats = parse_imu_csv(imu_csv)
    if not quats:
        return []
    fps = float(fps) if fps and fps > 0 else 25.0
    n_frames = int(n_frames)
    if n_frames <= 0:
        n_frames = len(quats)

    # Normalize times to seconds.
    if time_base is not None:
        src_t = [t * float(time_base) for t in times]
    elif max(times) <= (n_frames + 2):
        # scale in "source frames" (high-rate stream / index fallback) -> s
        src_t = [t / fps for t in times]
    else:
        src_t = list(times)  # already in seconds (perframe µs converted)

    # Canonicalize sign for a continuous slerp (double cover).
    canon = [list(quats[0])]
    for q in quats[1:]:
        if _quat_dot(canon[-1], q) < 0:
            q = [-c for c in q]
        canon.append(list(q))

    out = []
    j = 0
    m = len(src_t)
    for i in range(n_frames):
        t = i / fps
        if t <= src_t[0]:
            out.append(list(canon[0]))
            continue
        if t >= src_t[-1]:
            out.append(list(canon[-1]))
            continue
        while j + 1 < m and src_t[j + 1] < t:
            j += 1
        # bound the interval [j, j+1] containing t
        while j > 0 and src_t[j] > t:
            j -= 1
        t0, t1 = src_t[j], src_t[min(j + 1, m - 1)]
        if t1 <= t0:
            out.append(list(canon[j]))
            continue
        a = (t - t0) / (t1 - t0)
        out.append(slerp(canon[j], canon[min(j + 1, m - 1)], a))
    return out


# --- v360 euler decomposition ------------------------------------------------

def _v360_euler_from_matrix(Rc):
    """Decodes a content rotation ``Rc`` (3x3) into (yaw, pitch, roll) degrees
    such that the v360 filter (Vmat(yaw,pitch,roll)=Ry(yaw)Rx(-pitch)Rz(roll))
    reproduces ``Rc``. Convention validated at render (see header §5)."""
    np_ = _as_np()
    R = np_.asarray(Rc, dtype=float)
    # Vmat = [[.., .., sy*cp],[cp*sr, cp*cr, sp],[.., .., cy*cp]]  (see header)
    sp = max(-1.0, min(1.0, float(R[1, 2])))
    pitch_rad = math.asin(sp)
    cp = math.cos(pitch_rad)
    if abs(cp) > 1e-6:
        roll_rad = math.atan2(float(R[1, 0]), float(R[1, 1]))   # cp*sr, cp*cr
        yaw_rad = math.atan2(float(R[0, 2]), float(R[2, 2]))    # sy*cp, cy*cp
    else:  # gimbal lock (pitch ≈ ±90°): yaw absorbs, roll = 0
        roll_rad = 0.0
        yaw_rad = math.atan2(-float(R[2, 0]), float(R[0, 0]))
    return math.degrees(yaw_rad), -math.degrees(pitch_rad), math.degrees(roll_rad)


def _minimal_rotation(a, b):
    """Minimal rotation (3x3 matrix) sending unit vector a onto b."""
    np_ = _as_np()
    a = np_.asarray(a, float)
    a = a / np_.linalg.norm(a)
    b = np_.asarray(b, float)
    b = b / np_.linalg.norm(b)
    v = np_.cross(a, b)
    c = float(np_.dot(a, b))
    s = float(np_.linalg.norm(v))
    if s < 1e-9:
        return np_.eye(3) if c > 0 else np_.diag([1.0, -1.0, -1.0])
    vx = np_.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np_.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


def _rotmat_from_axis_angle_slerp(R, strength):
    """Attenuates a rotation R (matrix) by a factor ``strength`` in [0,1]
    (slerp between identity and R along its axis)."""
    np_ = _as_np()
    if strength >= 0.999:
        return R
    if strength <= 0.001:
        return np_.eye(3)
    # angle/axis
    ang = math.acos(max(-1.0, min(1.0, (np_.trace(R) - 1.0) / 2.0)))
    if ang < 1e-6:
        return np_.eye(3)
    ax = np_.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    n = np_.linalg.norm(ax)
    if n < 1e-9:
        return R
    ax = ax / n
    a = ang * strength
    K = np_.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
    return np_.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * (K @ K)


def _smooth_quaternions(quats, window):
    """Sliding average (by iterative slerp) of the orientations -> smoothed
    orientation per frame. ``window`` = half-window in frames."""
    n = len(quats)
    if window < 1 or n == 0:
        return [list(q) for q in quats]
    out = []
    for i in range(n):
        lo = max(0, i - window)
        hi = min(n - 1, i + window)
        acc = list(quats[lo])
        cnt = 1
        for k in range(lo + 1, hi + 1):
            cnt += 1
            acc = slerp(acc, quats[k], 1.0 / cnt)  # incremental average
        out.append(acc)
    return out


# --- Core: per-frame correction matrices -------------------------------------

def _correction_matrices(quats, mode, strength, ref_index, fps):
    """List of ``Rc`` matrices (content rotation to apply in equirect)."""
    np_ = _as_np()
    M = np_.array(_BODY_TO_EQUIRECT)
    down = np_.array(WORLD_DOWN)
    strength = max(0.0, min(1.0, float(strength)))
    mats = []

    if mode == "horizon":
        for q in quats:
            Rbw = _quat_to_matrix(q)
            g_e = M @ (Rbw.T @ down)          # "down" gravity seen in equirect
            Rc = _minimal_rotation(g_e, np_.array([0.0, -1.0, 0.0]))
            mats.append(_rotmat_from_axis_angle_slerp(Rc, strength))
        return mats

    if mode == "lock":
        ri = max(0, min(len(quats) - 1, int(ref_index)))
        Rref = _quat_to_matrix(quats[ri])
        for q in quats:
            Rbw = _quat_to_matrix(q)
            # world content locked to the reference: Rc = M·Rref^T·Rbw·M^T
            Rc = M @ (Rref.T @ (Rbw @ M.T))
            mats.append(_rotmat_from_axis_angle_slerp(Rc, strength))
        return mats

    if mode == "smooth":
        # strength -> half-window: 0 => ~0.1 s, 1 => ~1.5 s of smoothing.
        half = int(round((0.1 + 1.4 * strength) * fps * 0.5))
        half = max(1, half)
        sm = _smooth_quaternions(quats, half)
        for q, qs in zip(quats, sm):
            Rbw = _quat_to_matrix(q)
            Rs = _quat_to_matrix(qs)
            # follows the smoothed orientation: Rc = M·Rs^T·Rbw·M^T
            Rc = M @ (Rs.T @ (Rbw @ M.T))
            mats.append(Rc)   # smoothing IS the strength; no attenuation here
        return mats

    raise ValueError(f"unknown stabilization mode: {mode!r}")


def _vmat(yaw, pitch, roll):
    """Matrix applied by the v360 filter: Ry(yaw)·Rx(pitch)·Rz(roll)
    (convention validated at render, see header §5). Exact inverse of
    ``_v360_euler_from_matrix``."""
    np_ = _as_np()
    y = math.radians(yaw)
    p = math.radians(pitch)
    r = math.radians(roll)
    Ry = np_.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0],
                    [-math.sin(y), 0, math.cos(y)]])
    Rx = np_.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)],
                    [0, math.sin(p), math.cos(p)]])
    Rz = np_.array([[math.cos(r), -math.sin(r), 0],
                    [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


def compute_corrections(quats, mode="horizon", params=None):
    """(yaw, pitch, roll) in degrees (v360 frame, e:e filter) per frame.

    ``params``: optional dict ``{strength in [0,1], ref_index, fps,
    fold_baseline_yaw}``. ``fold_baseline_yaw`` (v360 mode) folds the rotation
    into the dfisheye stitch's v360 by composing with Vmat(yaw_base,0,0)
    (default 90°). Without it (calibrated mode), the eulers drive a v360=e:e.
    Returns ``[]`` if ``quats`` is empty (no IMU).
    """
    if not quats:
        return []
    if mode not in VALID_MODES:
        raise ValueError(f"unknown stabilization mode: {mode!r}")
    params = params or {}
    strength = params.get("strength", 1.0)
    ref_index = params.get("ref_index", 0)
    fps = float(params.get("fps", 25.0) or 25.0)
    fold_yaw = params.get("fold_baseline_yaw", None)

    mats = _correction_matrices(quats, mode, strength, ref_index, fps)
    base = _vmat(fold_yaw, 0.0, 0.0) if fold_yaw is not None else None
    out = []
    for Rc in mats:
        # Fold into the dfisheye stitch's v360: the sampling composes
        # d_out --Vmat(corr)=Rc--> d_mid --Vmat(90,0,0)=base--> d_fisheye, so
        # the single matrix is base @ Rc (Rc applied first).
        R = base @ Rc if base is not None else Rc
        out.append(_v360_euler_from_matrix(R))
    return out


def frame_corrections(imu_csv, fps, n_frames, mode="horizon", strength=1.0,
                      time_base=None, fold_baseline_yaw=None, ref_index=0):
    """Loads the IMU, computes the per-frame corrections and the ``has_imu`` flag.

    ``fold_baseline_yaw``: pass ``BASELINE_YAW_DEG`` (90) in v360 mode to
    fold the rotation into the stitch's v360; ``None`` in calibrated mode
    (dedicated v360=e:e). Returns a ``StabilizationResult``.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown stabilization mode: {mode!r}")
    quats = load_orientations(imu_csv, fps, n_frames, time_base)
    has_imu = bool(quats)
    if not has_imu:
        return StabilizationResult(corrections=[], has_imu=False, mode=mode,
                                   n_frames=int(n_frames))
    params = {"strength": strength, "ref_index": ref_index, "fps": fps,
              "fold_baseline_yaw": fold_baseline_yaw}
    corr = compute_corrections(quats, mode, params)
    return StabilizationResult(corrections=corr, has_imu=True, mode=mode,
                               n_frames=len(corr))


# --- sendcmd script generation -----------------------------------------------

def build_sendcmd(corrections, fps, out_path):
    """Writes a ``sendcmd`` command file driving a v360 filter.

    One command per frame, timestamped at ``(i-0.5)/fps`` s, setting yaw/pitch/roll.
    The targeted v360 filter MUST be the only v360 in the graph (see header).
    Returns ``out_path``. Empty corrections -> minimal file (no rotation).

    ================================================================================
    TEMPORAL DRIFT FIX — the v360 commands COMPOSE (post-mult).
    ================================================================================
    MEASUREMENT (see work/engine/stab/): a ``v360=e:e`` filter driven by sendcmd
    does NOT reset its orientation to the commanded value; it COMPOSES it with the
    current state, by POST-multiplication:

        S_i = S_{i-1} @ Vmat(commanded_angles_i)         (init S_{-1} = identity)

    Evidence: static marker + ``roll 30`` sent at EVERY frame -> the marker
    rotates by 30°/frame (accumulation) instead of staying at 30°; sent ONCE ->
    it goes to 30° and stays there. Sending the ABSOLUTE correction at each frame
    (old code) therefore accumulated the image rotations frame after frame:
    leveled at the beginning, growing drift up to ~90° at the end of the clip. That
    was the root cause of the drift.

    FIX: we send at each frame the rotation DELTA that brings the accumulated
    state from the previous target to the current target. With ``T_i = Vmat(corr_i)``
    (v360 matrix of the wanted absolute correction):

        C_i = T_{i-1}^T @ T_i        (T_{-1} = identity, so C_0 = T_0)

    Since v360 does ``S_i = S_{i-1} @ Vmat(C_i)`` and ``Vmat(_v360_euler_from_matrix(C_i))
    = C_i``, the product telescopes: ``S_i = T_0 (T_0^T T_1)(T_1^T T_2)... = T_i``.
    The orientation applied to frame i is therefore EXACTLY the absolute target T_i.
    Verified at render: reproduces the absolute target to the pixel (diff 0.00) on
    frames 40/80/120/156, and the horizon stays leveled from start to end of the real clip.
    """
    fps = float(fps) if fps and fps > 0 else 25.0
    lines = []
    if corrections:
        np_ = _as_np()
        prev = np_.eye(3)                       # starting state = neutral rotation
        for i, (yaw, pitch, roll) in enumerate(corrections):
            t_i = _vmat(yaw, pitch, roll)       # absolute target T_i
            c_i = prev.T @ t_i                  # delta to compose (v360 post-mult)
            dyaw, dpitch, droll = _v360_euler_from_matrix(c_i)
            prev = t_i
            # The command for frame i is TRIGGERED half a frame early
            # (t = (i-0.5)/fps): each frame (PTS = i/fps) thus reliably falls
            # AFTER the triggering of ITS command and BEFORE that of the
            # next, despite floating PTS jitter. An instantaneous interval
            # exactly at i/fps can be missed (edge bug) and leave the frame
            # on a stale command — visible in fast motion areas.
            t = max(0.0, (i - 0.5) / fps)
            lines.append(
                f"{t:.6f} v360 yaw {dyaw:.4f}, v360 pitch {dpitch:.4f}, "
                f"v360 roll {droll:.4f};"
            )
    with open(out_path, "w") as fp:
        fp.write("\n".join(lines))
        if lines:
            fp.write("\n")
    return out_path

"""Generation of ffmpeg `remap` maps from the DJI Osmo 360 factory calibration.

Produces, for each lens, an xmap/ymap pair in 16-bit PGM (consumed by the
ffmpeg ``remap`` filter) that projects the source fisheye to the output
equirectangular, as well as a blending mask (8-bit PGM, soft gradient ±5° around
the seams) for ``maskedmerge``.

Interpretation of the embedded calibration (validated empirically against the
equirectangular thumbnail stitched by the camera — see work/engine/):

- ``extrinsic_quat`` = quaternion ``[w, x, y, z]`` such that ``v_lens = R(q)·v_body``.
  Body frame: X right, Y forward, Z up. Lens frame:
  X = image x, Y = image y (downward), Z = outgoing optical axis.
  The two lenses are indeed related by ~180° around Z (front/back).
- Video streams 0:0 and 0:1 correspond to ``lenses[0]`` (yaw≈-180°, back) and
  ``lenses[1]`` (yaw≈0°, front) respectively.
- Fisheye projection: r(θ) = s·fx·g(θ) with g(θ) = θ + k1·θ³ + k2·θ⁵ + k3·θ⁷ + k4·θ⁹
  (OpenCV-fisheye-like model, ``dist`` coefficients). This polynomial is NOT
  monotonic beyond ~88°: it is extended linearly (tangent) beyond
  THETA_LIN = 85°.
- The two "radial LUTs" are not an angle→radius curve: the pairs
  ``(radial_lut_1[i], radial_lut_2[i])``, i=1..13, describe a CIRCLE of radius
  ≈1815–1860 px around (cx, cy) — the seam circle at θ=90°, sampled
  at azimuths 30°..150° in 10° steps. The raw polynomial underestimates this
  radius by about 11%; the per-lens scale is therefore recalibrated on it:
  s = mean_LUT_radius / (fx·g_ext(π/2)). With this recalibration the render sticks
  to the camera thumbnail (empirical optimum s≈1.115, derived value s≈1.115 too).
- The +90° longitude offset (YAW_OFFSET_DEG) aligns the output with the
  v360 baseline (``yaw=90``) and with the embedded thumbnail.

Fallback ``calibration=None``: ideal dfisheye geometry (equidistant, FOV 190°,
centers at the middle of the image), nearest equivalent of the v360 mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

# Field covered per lens retained for the maps (beyond: invalid/black).
FOV_HALF_DEG = 96.0
# Half-band of blending around the seam at 90° (total band = 10°).
FADE_HALF_DEG = 5.0
# Angle beyond which the distortion polynomial is extended linearly.
THETA_LIN_DEG = 85.0
# Longitude alignment on the v360 baseline (yaw=90) and the camera thumbnail.
YAW_OFFSET_DEG = 90.0
# "Out of field" value of the remap maps (>= source dimensions => black pixel).
INVALID = 65535
# Rows processed per chunk (limits memory to ~8k wide).
CHUNK_ROWS = 256


@dataclass
class MapSet:
    """Remap maps + blending mask for a pair of fisheyes."""

    out_w: int
    out_h: int
    xmaps: list[str] = field(default_factory=list)  # [lens0(back), lens1(front)]
    ymaps: list[str] = field(default_factory=list)
    blend_mask: str = ""   # gray PGM: weight of lens 1 (front) for maskedmerge
    calibrated: bool = False  # False = ideal-geometry fallback (prefer v360)


def _quat_to_rot(q: list[float]) -> np.ndarray:
    """Quaternion [w,x,y,z] -> 3x3 matrix such that v' = R·v."""
    w, x, y, z = q
    n = (w * w + x * x + y * y + z * z) ** 0.5
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def _radial_model(lens: dict):
    """Return r(θ) in pixels for a calibrated lens.

    OpenCV-fisheye odd polynomial extended linearly beyond THETA_LIN_DEG,
    rescaled on the seam circle described by the radial LUTs.
    """
    fx = float(lens["fx"])
    k1, k2, k3, k4 = (float(k) for k in lens["dist"])
    t0 = np.radians(THETA_LIN_DEG)

    def g(t):
        return t + k1 * t**3 + k2 * t**5 + k3 * t**7 + k4 * t**9

    def g_prime(t):
        return 1 + 3 * k1 * t**2 + 5 * k2 * t**4 + 7 * k3 * t**6 + 9 * k4 * t**8

    g0, gp0 = float(g(t0)), float(g_prime(t0))

    def g_ext(t):
        return np.where(t <= t0, g(t), g0 + gp0 * (t - t0))

    # Scale: LUT seam circle (mean radius around (cx,cy)) = r(90°).
    scale = 1.0
    lut1, lut2 = lens.get("radial_lut_1"), lens.get("radial_lut_2")
    if lut1 and lut2 and len(lut1) >= 14 and len(lut2) >= 14:
        pts_r = np.hypot(np.asarray(lut1[1:]) - lens["cx"],
                         np.asarray(lut2[1:]) - lens["cy"])
        r90 = fx * float(g_ext(np.pi / 2))
        if r90 > 0:
            scale = float(pts_r.mean()) / r90

    return lambda theta: scale * fx * g_ext(theta)


def _ideal_lenses(src_w: float = 3840.0, src_h: float = 3840.0) -> list[dict]:
    """Fallback without calibration: ideal equidistant dfisheye, FOV 190°."""
    # image circle ~3735/3840 of the width (measured on the Osmo 360)
    r95 = 0.5 * min(src_w, src_h) * (3735.0 / 3840.0)
    lenses = []
    for qz in ((0.0, 1.0), (1.0, 0.0)):  # back: 180° then front: 0° around Z
        # quaternion [w,x,y,z]: body->lens rotation = R_x(90°) (front)
        # or R_z(180°)·R_x(90°) (back)
        if qz[1] == 0.0:  # front: +90° rotation around X
            quat = [np.cos(np.pi / 4), np.sin(np.pi / 4), 0.0, 0.0]
        else:  # back: 180° around axis (0, -√2/2, √2/2)
            quat = [0.0, 0.0, -np.sin(np.pi / 4), np.cos(np.pi / 4)]
        lenses.append({
            "fx": r95 / np.radians(95.0), "fy": r95 / np.radians(95.0),
            "cx": src_w / 2.0 - 0.5, "cy": src_h / 2.0 - 0.5,
            "dist": [0.0, 0.0, 0.0, 0.0],
            "width": src_w, "height": src_h,
            "extrinsic_quat": quat,
            "_ideal": True,
        })
    return lenses


def _write_pgm(path: str, data: np.ndarray, maxval: int) -> None:
    dt = ">u2" if maxval > 255 else "u1"
    h, w = data.shape
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n{maxval}\n".encode())
        f.write(np.ascontiguousarray(data.astype(dt)).tobytes())


def generate_remap_maps(calibration: dict | None, out_w: int, out_h: int,
                        workdir: str) -> MapSet:
    """Generate 16-bit xmap/ymap per lens + blending mask, in PGM.

    ``calibration``: contents of calibration.json ("lenses" key) or None
    (ideal-geometry fallback). Equirectangular output ``out_w`` x ``out_h``.
    """
    os.makedirs(workdir, exist_ok=True)
    calibrated = bool(calibration and calibration.get("lenses"))
    if calibrated:
        lenses = calibration["lenses"][:2]
    else:
        lenses = _ideal_lenses()

    models = []
    rot = []
    for lens in lenses:
        if lens.get("_ideal"):
            fx = lens["fx"]
            models.append(lambda t, fx=fx: fx * t)
        else:
            models.append(_radial_model(lens))
        rot.append(_quat_to_rot(lens["extrinsic_quat"]))

    xmaps = [np.empty((out_h, out_w), np.uint16) for _ in lenses]
    ymaps = [np.empty((out_h, out_w), np.uint16) for _ in lenses]
    mask = np.empty((out_h, out_w), np.uint8)

    lon = ((np.arange(out_w) + 0.5) / out_w * 2 * np.pi - np.pi
           + np.radians(YAW_OFFSET_DEG))
    theta_max = np.radians(FOV_HALF_DEG)
    a0 = np.radians(90.0 - FADE_HALF_DEG)
    a1 = np.radians(90.0 + FADE_HALF_DEG)

    for y0 in range(0, out_h, CHUNK_ROWS):
        y1 = min(y0 + CHUNK_ROWS, out_h)
        lat = np.pi / 2 - (np.arange(y0, y1) + 0.5) / out_h * np.pi
        cl, sl = np.cos(lat)[:, None], np.sin(lat)[:, None]
        # world direction (body frame): X right, Y forward, Z up
        d = np.stack([cl * np.sin(lon)[None, :],
                      cl * np.cos(lon)[None, :],
                      np.broadcast_to(sl, (y1 - y0, out_w))], axis=-1).astype(np.float32)
        weights = []
        for i, lens in enumerate(lenses):
            dl = d @ rot[i].T.astype(np.float32)
            theta = np.arccos(np.clip(dl[..., 2], -1.0, 1.0))
            rho = np.hypot(dl[..., 0], dl[..., 1])
            rho = np.maximum(rho, 1e-12)
            r = models[i](theta)
            px = lens["cx"] + r * dl[..., 0] / rho
            py = lens["cy"] + r * dl[..., 1] / rho
            w_src, h_src = float(lens["width"]), float(lens["height"])
            valid = ((theta < theta_max) & (px >= 0) & (px <= w_src - 1)
                     & (py >= 0) & (py <= h_src - 1))
            xmaps[i][y0:y1] = np.where(valid, px.round(), INVALID).astype(np.uint16)
            ymaps[i][y0:y1] = np.where(valid, py.round(), INVALID).astype(np.uint16)
            wgt = np.clip((a1 - theta) / (a1 - a0), 0.0, 1.0)
            weights.append(np.where(valid, wgt, 0.0))
        wsum = weights[0] + weights[1]
        m = np.where(wsum > 0, weights[1] / np.maximum(wsum, 1e-12), 0.5)
        mask[y0:y1] = np.round(m * 255.0).astype(np.uint8)

    ms = MapSet(out_w=out_w, out_h=out_h, calibrated=calibrated)
    for i in range(len(lenses)):
        xp = os.path.join(workdir, f"xmap{i}.pgm")
        yp = os.path.join(workdir, f"ymap{i}.pgm")
        _write_pgm(xp, xmaps[i], INVALID)
        _write_pgm(yp, ymaps[i], INVALID)
        ms.xmaps.append(xp)
        ms.ymaps.append(yp)
    ms.blend_mask = os.path.join(workdir, "blend_mask.pgm")
    _write_pgm(ms.blend_mask, mask, 255)
    return ms

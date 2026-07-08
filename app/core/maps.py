"""Génération des cartes `remap` ffmpeg depuis la calibration usine DJI Osmo 360.

Produit, pour chaque objectif, une paire xmap/ymap en PGM 16 bits (consommée par
le filtre ffmpeg ``remap``) qui projette le fisheye source vers l'équirectangulaire
de sortie, ainsi qu'un masque de fusion (PGM 8 bits, dégradé doux ±5° autour des
coutures) pour ``maskedmerge``.

Interprétation de la calibration embarquée (validée empiriquement contre la
miniature équirectangulaire stitchée par la caméra — voir work/engine/) :

- ``extrinsic_quat`` = quaternion ``[w, x, y, z]`` tel que ``v_objectif = R(q)·v_boîtier``.
  Repère boîtier : X à droite, Y vers l'avant, Z vers le haut. Repère objectif :
  X = x image, Y = y image (vers le bas), Z = axe optique sortant.
  Les deux objectifs sont bien reliés par ~180° autour de Z (avant/arrière).
- Les flux vidéo 0:0 et 0:1 correspondent à ``lenses[0]`` (yaw≈-180°, dos) et
  ``lenses[1]`` (yaw≈0°, face) respectivement.
- Projection fisheye : r(θ) = s·fx·g(θ) avec g(θ) = θ + k1·θ³ + k2·θ⁵ + k3·θ⁷ + k4·θ⁹
  (modèle type OpenCV-fisheye, coefficients ``dist``). Ce polynôme n'est PAS
  monotone au-delà de ~88° : on le prolonge linéairement (tangente) au-delà de
  THETA_LIN = 85°.
- Les deux « LUT radiales » ne sont pas une courbe angle→rayon : les couples
  ``(radial_lut_1[i], radial_lut_2[i])``, i=1..13, décrivent un CERCLE de rayon
  ≈1815–1860 px autour de (cx, cy) — le cercle de couture à θ=90°, échantillonné
  aux azimuts 30°..150° par pas de 10°. Le polynôme brut sous-estime ce rayon
  d'environ 11 % ; l'échelle par objectif est donc recalée dessus :
  s = rayon_moyen_LUT / (fx·g_ext(π/2)). Avec ce recalage le rendu colle à la
  miniature caméra (optimum empirique s≈1.115, valeur dérivée s≈1.115 aussi).
- Le décalage de +90° de longitude (YAW_OFFSET_DEG) aligne la sortie sur la
  baseline v360 (``yaw=90``) et sur la miniature embarquée.

Fallback ``calibration=None`` : géométrie dfisheye idéale (équidistant, FOV 190°,
centres au milieu de l'image), équivalent nearest du mode v360.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

# Champ couvert par objectif retenu pour les cartes (au-delà : invalide/noir).
FOV_HALF_DEG = 96.0
# Demi-bande de fusion autour de la couture à 90° (bande totale = 10°).
FADE_HALF_DEG = 5.0
# Angle au-delà duquel le polynôme de distorsion est prolongé linéairement.
THETA_LIN_DEG = 85.0
# Alignement en longitude sur la baseline v360 (yaw=90) et la miniature caméra.
YAW_OFFSET_DEG = 90.0
# Valeur "hors champ" des cartes remap (>= dimensions source => pixel noir).
INVALID = 65535
# Lignes traitées par tranche (limite la mémoire à ~8k de large).
CHUNK_ROWS = 256


@dataclass
class MapSet:
    """Cartes remap + masque de fusion pour une paire de fisheyes."""

    out_w: int
    out_h: int
    xmaps: list[str] = field(default_factory=list)  # [objectif0(dos), objectif1(face)]
    ymaps: list[str] = field(default_factory=list)
    blend_mask: str = ""   # PGM gris : poids de l'objectif 1 (face) pour maskedmerge
    calibrated: bool = False  # False = fallback géométrie idéale (préférer v360)


def _quat_to_rot(q: list[float]) -> np.ndarray:
    """Quaternion [w,x,y,z] -> matrice 3x3 telle que v' = R·v."""
    w, x, y, z = q
    n = (w * w + x * x + y * y + z * z) ** 0.5
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def _radial_model(lens: dict):
    """Retourne r(θ) en pixels pour un objectif calibré.

    Polynôme impair OpenCV-fisheye prolongé linéairement au-delà de THETA_LIN_DEG,
    recalé en échelle sur le cercle de couture décrit par les LUT radiales.
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

    # Échelle : cercle de couture LUT (rayon moyen autour de (cx,cy)) = r(90°).
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
    """Fallback sans calibration : dfisheye idéal équidistant, FOV 190°."""
    # cercle image ~3735/3840 de la largeur (mesuré sur l'Osmo 360)
    r95 = 0.5 * min(src_w, src_h) * (3735.0 / 3840.0)
    lenses = []
    for qz in ((0.0, 1.0), (1.0, 0.0)):  # dos: 180° puis face: 0° autour de Z
        # quaternion [w,x,y,z] : rotation boîtier->objectif = R_x(90°) (face)
        # ou R_z(180°)·R_x(90°) (dos)
        if qz[1] == 0.0:  # face : rotation +90° autour de X
            quat = [np.cos(np.pi / 4), np.sin(np.pi / 4), 0.0, 0.0]
        else:  # dos : 180° autour de l'axe (0, -√2/2, √2/2)
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
    """Génère xmap/ymap 16 bits par objectif + masque de fusion, en PGM.

    ``calibration`` : contenu de calibration.json (clé "lenses") ou None
    (fallback géométrie idéale). Sortie équirectangulaire ``out_w`` x ``out_h``.
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
        # direction monde (repère boîtier) : X droite, Y avant, Z haut
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

"""Stabilisation gyroscopique des .OSV DJI Osmo 360 (phase 2 — activation).

Objectif : niveler l'horizon et lisser les secousses en contre-tournant chaque
frame de la sortie équirectangulaire selon l'orientation IMU embarquée (~1 kHz
quaternions extraits par ``osv_meta/extract_djmd.py``).

================================================================================
CONVENTION D'AXES — VALIDÉE EMPIRIQUEMENT (clip à main levée réel, 6,3 s :
CAM_20260708072843_0001_D.OSV) — voir work/engine/stab/ pour les preuves.
================================================================================

1. QUATERNION SOURCE (imu_perframe / imu_highrate) : ordre ``[w, x, y, z]``,
   normalisé (||q|| = 1.0000). Il représente l'orientation BOITIER -> MONDE :
   ``v_monde = R(q) · v_boitier``.

   Preuve : l'accéléromètre (champ ``ax_g,ay_g,az_g``, en g, mesure la réaction
   à la gravité => pointe vers le HAUT local dans le repère boitier). En testant
   les 4 combinaisons {ordre wxyz|xyzw} × {R|Rᵀ}, seule ``[w,x,y,z]`` avec R
   donne un vecteur ``R(q)·accel_boitier`` CONSTANT dans le temps (dispersion
   moyenne ~11°, le résidu étant l'accélération dynamique réelle du clip qui
   bouge de 144°) ; les autres conventions donnent 29–44° de dispersion. Ce
   vecteur constant vaut ≈ [0, 0, -0.95] : l'axe vertical du MONDE est -Z
   (gravité « bas » = +Z monde). Recoupé image par image : la gravité déduite du
   quaternion coïncide avec l'accéléromètre à 1–8° près sur les frames lentes.

2. REPÈRE MONDE DJI : Z pointe vers le BAS (sens de la gravité), plan XY
   horizontal. (``WORLD_DOWN = [0, 0, 1]``.)

3. REPÈRE ÉQUIRECT de sortie (= convention du filtre v360, output=e) :
   Xe = droite, Ye = HAUT (zénith en haut de l'image), Ze = AVANT (centre de
   l'image). yaw tourne autour de Ye, pitch autour de Xe, roll autour de Ze.

4. MATRICE BOITIER -> ÉQUIRECT (``BODY_TO_EQUIRECT`` = M), telle que produite par
   le stitch baseline ``v360=input=dfisheye:...:yaw=90`` :
       Y_boitier (avant)  -> Ze  (centre de l'image)
       X_boitier          -> -Ye (nadir / bas)
       Z_boitier          -> -Xe
   Vérifiée : à la frame la plus inclinée (135), la gravité projetée en équirect
   donne un roll prédit ≈ +35° qui, appliqué, remet la personne à la verticale
   et l'horizon à plat (rendus base_f135.jpg vs corr_f135A.jpg). À la frame 60
   (caméra pointée vers le ciel, correction ~114°) la même chaîne redresse la
   scène (c60_c2.jpg).

5. CONVENTION DE ROTATION DU FILTRE v360 (rorder="ypr", validée au rendu sur
   des corrections allant jusqu'à ~114° — frames 135 et 60) : le filtre applique
   aux directions la matrice
       Vmat(yaw, pitch, roll) = Ry(yaw) · Rx(pitch) · Rz(roll)
   (yaw autour de Ye, pitch autour de Xe, roll autour de Ze ; yaw « externe »,
   cohérent avec l'ordre ypr). Pour réaliser une rotation de contenu ``Rc`` on
   résout ``Vmat(yaw,pitch,roll) = Rc`` (décodage fermé, exact,
   ``_v360_euler_from_matrix``). Cette convention reproduit correctement les
   GRANDES corrections multi-axes, là où une décomposition d'ordre inversé
   échouait.

   IMPORTANT (mécanique sendcmd) : les commandes yaw/pitch/roll du filtre v360
   se COMPOSENT avec l'orientation d'INITIALISATION du filtre. Le v360 piloté
   par sendcmd DOIT donc être initialisé en rotation NEUTRE (yaw=0:pitch=0:
   roll=0) ; l'alignement baseline yaw=90 est alors intégré dans les angles
   envoyés (cf. ``fold_baseline_yaw``). Sans cela, le rendu est faussé.

--------------------------------------------------------------------------------
Application dans la chaîne ffmpeg (voir stitch.py) — un SEUL filtre v360 par
graphe pour que ``sendcmd`` cible sans ambiguïté (le ciblage par type de filtre
est ambigu quand deux v360 coexistent) :
  * mode v360 : la rotation de stabilisation est FONDUE dans le v360 du stitch
    (dfisheye->e). On envoie par sendcmd, image par image, les euler de
    ``Rc · Vmat(90,0,0)`` (Vmat(90,0,0) = l'alignement baseline yaw=90). Évite
    aussi un second rééchantillonnage.
  * mode calibrated : le stitch utilise ``remap`` (aucun v360) ; on ajoute un
    v360=e:e dédié, unique, piloté par sendcmd avec les euler de ``Rc``.
--------------------------------------------------------------------------------
Absence d'IMU : ``frame_corrections`` renvoie ``([], has_imu=False)`` et le
pipeline n'insère aucune stabilisation (corrections nulles).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy est une dépendance déclarée
    np = None  # type: ignore


# --- Conventions validées (voir en-tête) ------------------------------------

WORLD_DOWN = (0.0, 0.0, 1.0)          # gravité « bas » dans le repère monde DJI
# Boitier -> équirect (lignes = composantes Xe, Ye, Ze ; colonnes = X,Y,Z boitier)
_BODY_TO_EQUIRECT = (
    (0.0, 0.0, -1.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
)
# yaw de la baseline v360 (aligne dfisheye + miniature caméra) fondu en mode v360.
BASELINE_YAW_DEG = 90.0

VALID_MODES = ("horizon", "lock", "smooth")


@dataclass
class StabilizationResult:
    """Résultat prêt à injecter dans ffmpeg."""
    corrections: list           # liste de (yaw, pitch, roll) en degrés (repère v360)
    has_imu: bool
    mode: str
    n_frames: int


# --- Petites algèbres quaternion / matrice (numpy si dispo) ------------------

def _as_np():
    if np is None:  # pragma: no cover
        raise RuntimeError("numpy requis pour la stabilisation")
    return np


def _quat_to_matrix(q):
    """q = [w, x, y, z] (unitaire) -> matrice 3x3 R telle que v_monde = R·v_boitier."""
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
    """Interpolation sphérique entre deux quaternions [w,x,y,z]."""
    q0 = _quat_normalize(list(q0))
    q1 = _quat_normalize(list(q1))
    d = _quat_dot(q0, q1)
    if d < 0.0:  # chemin le plus court (double couverture)
        q1 = [-c for c in q1]
        d = -d
    if d > 0.9995:  # quasi colinéaires -> interpolation linéaire
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


# --- Lecture CSV IMU ---------------------------------------------------------

def parse_imu_csv(path):
    """Lit un CSV IMU (imu_highrate.csv OU imu_perframe.csv).

    Retourne ``(times_s, quats)`` : ``times_s`` liste de temps relatifs (s,
    origine = première ligne), ``quats`` liste de [w,x,y,z]. ``fps`` sert à
    dater le flux haute fréquence (qui n'a pas d'horodatage absolu). Retourne
    ``([], [])`` si aucune donnée exploitable.
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

    # Construction de l'échelle de temps relative (secondes).
    times = None
    if has_ts and all(t is not None for t in raw_ts) and len(raw_ts) == len(quats):
        t0 = raw_ts[0]
        # timestamps en microsecondes (delta ~40002 µs @ 25 fps).
        times = [(t - t0) * 1e-6 for t in raw_ts]
        if times[-1] <= 0:  # unité inattendue -> repli index
            times = None
    if times is None and has_sub and frames:
        # flux haute fréquence : dater par (frame + subidx/n_sub_frame)/fps.
        per_frame = {}
        for f in frames:
            per_frame[f] = per_frame.get(f, 0) + 1
        # fps posé à 1.0 ici ; re-daté proprement dans load_orientations qui
        # connaît la vraie cadence -> on renvoie un temps "en frames".
        times = []
        seen = {}
        for f, s in zip(frames, subs):
            cnt = max(1, per_frame.get(f, 1))
            times.append(f + s / cnt)  # unité = frames source
    if times is None:
        times = list(range(len(quats)))  # unité = frames source
    return times, quats


def load_orientations(imu_csv, fps, n_frames, time_base=None):
    """Une orientation (quaternion [w,x,y,z]) par frame de sortie.

    Rééchantillonne le flux IMU (slerp) aux instants des frames de sortie
    ``t_i = i / fps`` (i = 0..n_frames-1). ``time_base`` : si fourni, échelle
    (secondes/unité) appliquée aux temps sources ; sinon auto (les CSV en
    microsecondes sont déjà convertis, le flux haute fréquence est en frames et
    converti via ``fps``). Retourne ``[]`` si l'IMU est absente.
    """
    times, quats = parse_imu_csv(imu_csv)
    if not quats:
        return []
    fps = float(fps) if fps and fps > 0 else 25.0
    n_frames = int(n_frames)
    if n_frames <= 0:
        n_frames = len(quats)

    # Homogénéise les temps en secondes.
    if time_base is not None:
        src_t = [t * float(time_base) for t in times]
    elif max(times) <= (n_frames + 2):
        # échelle en "frames source" (flux haute fréquence / repli index) -> s
        src_t = [t / fps for t in times]
    else:
        src_t = list(times)  # déjà en secondes (perframe µs converti)

    # Canonicalise le signe pour un slerp continu (double couverture).
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
        # borne l'intervalle [j, j+1] contenant t
        while j > 0 and src_t[j] > t:
            j -= 1
        t0, t1 = src_t[j], src_t[min(j + 1, m - 1)]
        if t1 <= t0:
            out.append(list(canon[j]))
            continue
        a = (t - t0) / (t1 - t0)
        out.append(slerp(canon[j], canon[min(j + 1, m - 1)], a))
    return out


# --- Décomposition euler v360 ------------------------------------------------

def _v360_euler_from_matrix(Rc):
    """Décode une rotation de contenu ``Rc`` (3x3) en (yaw, pitch, roll) degrés
    tels que le filtre v360 (Vmat(yaw,pitch,roll)=Ry(yaw)Rx(-pitch)Rz(roll))
    reproduise ``Rc``. Convention validée au rendu (voir en-tête §5)."""
    np_ = _as_np()
    R = np_.asarray(Rc, dtype=float)
    # Vmat = [[.., .., sy*cp],[cp*sr, cp*cr, sp],[.., .., cy*cp]]  (cf. en-tête)
    sp = max(-1.0, min(1.0, float(R[1, 2])))
    pitch_rad = math.asin(sp)
    cp = math.cos(pitch_rad)
    if abs(cp) > 1e-6:
        roll_rad = math.atan2(float(R[1, 0]), float(R[1, 1]))   # cp*sr, cp*cr
        yaw_rad = math.atan2(float(R[0, 2]), float(R[2, 2]))    # sy*cp, cy*cp
    else:  # blocage de cardan (pitch ≈ ±90°) : yaw absorbe, roll = 0
        roll_rad = 0.0
        yaw_rad = math.atan2(-float(R[2, 0]), float(R[0, 0]))
    return math.degrees(yaw_rad), -math.degrees(pitch_rad), math.degrees(roll_rad)


def _minimal_rotation(a, b):
    """Rotation minimale (matrice 3x3) envoyant le vecteur unitaire a sur b."""
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
    """Atténue une rotation R (matrice) par un facteur ``strength`` in [0,1]
    (slerp entre identité et R le long de son axe)."""
    np_ = _as_np()
    if strength >= 0.999:
        return R
    if strength <= 0.001:
        return np_.eye(3)
    # angle/axe
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
    """Moyenne glissante (par slerp itératif) des orientations -> orientation
    lissée par frame. ``window`` = demi-fenêtre en frames."""
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
            acc = slerp(acc, quats[k], 1.0 / cnt)  # moyenne incrémentale
        out.append(acc)
    return out


# --- Cœur : matrices de correction par frame ---------------------------------

def _correction_matrices(quats, mode, strength, ref_index, fps):
    """Liste de matrices ``Rc`` (rotation de contenu à appliquer en équirect)."""
    np_ = _as_np()
    M = np_.array(_BODY_TO_EQUIRECT)
    down = np_.array(WORLD_DOWN)
    strength = max(0.0, min(1.0, float(strength)))
    mats = []

    if mode == "horizon":
        for q in quats:
            Rbw = _quat_to_matrix(q)
            g_e = M @ (Rbw.T @ down)          # gravité « bas » vue en équirect
            Rc = _minimal_rotation(g_e, np_.array([0.0, -1.0, 0.0]))
            mats.append(_rotmat_from_axis_angle_slerp(Rc, strength))
        return mats

    if mode == "lock":
        ri = max(0, min(len(quats) - 1, int(ref_index)))
        Rref = _quat_to_matrix(quats[ri])
        for q in quats:
            Rbw = _quat_to_matrix(q)
            # contenu monde figé sur la référence : Rc = M·Rref^T·Rbw·M^T
            Rc = M @ (Rref.T @ (Rbw @ M.T))
            mats.append(_rotmat_from_axis_angle_slerp(Rc, strength))
        return mats

    if mode == "smooth":
        # strength -> demi-fenêtre : 0 => ~0,1 s, 1 => ~1,5 s de lissage.
        half = int(round((0.1 + 1.4 * strength) * fps * 0.5))
        half = max(1, half)
        sm = _smooth_quaternions(quats, half)
        for q, qs in zip(quats, sm):
            Rbw = _quat_to_matrix(q)
            Rs = _quat_to_matrix(qs)
            # suit l'orientation lissée : Rc = M·Rs^T·Rbw·M^T
            Rc = M @ (Rs.T @ (Rbw @ M.T))
            mats.append(Rc)   # le lissage EST la force ; pas d'atténuation ici
        return mats

    raise ValueError(f"mode de stabilisation inconnu : {mode!r}")


def _vmat(yaw, pitch, roll):
    """Matrice appliquée par le filtre v360 : Ry(yaw)·Rx(pitch)·Rz(roll)
    (convention validée au rendu, voir en-tête §5). Inverse exact de
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
    """(yaw, pitch, roll) en degrés (repère v360, filtre e:e) par frame.

    ``params`` : dict optionnel ``{strength in [0,1], ref_index, fps,
    fold_baseline_yaw}``. ``fold_baseline_yaw`` (mode v360) fond la rotation
    dans le v360 du stitch dfisheye en composant avec Vmat(yaw_base,0,0)
    (défaut 90°). Sans lui (mode calibrated), les euler pilotent un v360=e:e.
    Retourne ``[]`` si ``quats`` est vide (absence d'IMU).
    """
    if not quats:
        return []
    if mode not in VALID_MODES:
        raise ValueError(f"mode de stabilisation inconnu : {mode!r}")
    params = params or {}
    strength = params.get("strength", 1.0)
    ref_index = params.get("ref_index", 0)
    fps = float(params.get("fps", 25.0) or 25.0)
    fold_yaw = params.get("fold_baseline_yaw", None)

    mats = _correction_matrices(quats, mode, strength, ref_index, fps)
    base = _vmat(fold_yaw, 0.0, 0.0) if fold_yaw is not None else None
    out = []
    for Rc in mats:
        # Fusion dans le v360 du stitch dfisheye : l'échantillonnage compose
        # d_out --Vmat(corr)=Rc--> d_mid --Vmat(90,0,0)=base--> d_fisheye, donc
        # la matrice unique vaut base @ Rc (Rc appliquée en premier).
        R = base @ Rc if base is not None else Rc
        out.append(_v360_euler_from_matrix(R))
    return out


def frame_corrections(imu_csv, fps, n_frames, mode="horizon", strength=1.0,
                      time_base=None, fold_baseline_yaw=None, ref_index=0):
    """Charge l'IMU, calcule les corrections par frame et le drapeau ``has_imu``.

    ``fold_baseline_yaw`` : passer ``BASELINE_YAW_DEG`` (90) en mode v360 pour
    fondre la rotation dans le v360 du stitch ; ``None`` en mode calibrated
    (v360=e:e dédié). Retourne un ``StabilizationResult``.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode de stabilisation inconnu : {mode!r}")
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


# --- Génération du script sendcmd -------------------------------------------

def build_sendcmd(corrections, fps, out_path):
    """Écrit un fichier de commandes ``sendcmd`` pilotant un filtre v360.

    Une commande par frame, datée à ``(i-0.5)/fps`` s, réglant yaw/pitch/roll.
    Le filtre v360 visé DOIT être l'unique v360 du graphe (cf. en-tête).
    Retourne ``out_path``. Corrections vide -> fichier minimal (aucune rotation).

    ================================================================================
    CORRECTIF DÉRIVE TEMPORELLE — les commandes v360 se COMPOSENT (post-mult).
    ================================================================================
    MESURE (voir work/engine/stab/) : un filtre ``v360=e:e`` piloté par sendcmd
    NE remet PAS son orientation à la valeur commandée ; il la COMPOSE avec l'état
    courant, en POST-multiplication :

        S_i = S_{i-1} @ Vmat(angles_commandés_i)         (init S_{-1} = identité)

    Preuve : marqueur statique + ``roll 30`` envoyé à CHAQUE frame -> le marqueur
    tourne de 30°/frame (accumulation) au lieu de rester à 30° ; envoyé UNE fois ->
    il va à 30° et y reste. Envoyer la correction ABSOLUE à chaque frame (ancien
    code) accumulait donc les rotations image après image : nivelé au début, dérive
    croissante jusqu'à ~90° en fin de clip. C'était la cause racine de la dérive.

    CORRECTIF : on envoie à chaque frame le DELTA de rotation qui amène l'état
    accumulé de la cible précédente à la cible courante. Avec ``T_i = Vmat(corr_i)``
    (matrice v360 de la correction absolue voulue) :

        C_i = T_{i-1}^T @ T_i        (T_{-1} = identité, donc C_0 = T_0)

    Comme v360 fait ``S_i = S_{i-1} @ Vmat(C_i)`` et ``Vmat(_v360_euler_from_matrix(C_i))
    = C_i``, le produit télescope : ``S_i = T_0 (T_0^T T_1)(T_1^T T_2)... = T_i``.
    L'orientation appliquée à la frame i vaut donc EXACTEMENT la cible absolue T_i.
    Vérifié au rendu : reproduit la cible absolue au pixel près (diff 0.00) sur
    frames 40/80/120/156, et l'horizon reste nivelé du début à la fin du vrai clip.
    """
    fps = float(fps) if fps and fps > 0 else 25.0
    lines = []
    if corrections:
        np_ = _as_np()
        prev = np_.eye(3)                       # état de départ = rotation neutre
        for i, (yaw, pitch, roll) in enumerate(corrections):
            t_i = _vmat(yaw, pitch, roll)       # cible absolue T_i
            c_i = prev.T @ t_i                  # delta à composer (post-mult v360)
            dyaw, dpitch, droll = _v360_euler_from_matrix(c_i)
            prev = t_i
            # La commande de la frame i est DÉCLENCHÉE une demi-frame en avance
            # (t = (i-0.5)/fps) : chaque frame (PTS = i/fps) tombe ainsi de façon
            # fiable APRÈS le déclenchement de SA commande et AVANT celui de la
            # suivante, malgré le jitter flottant des PTS. Un intervalle instantané
            # exactement à i/fps peut être manqué (bug de bord) et laisser la frame
            # sur une commande périmée — visible dans les zones de mouvement rapide.
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

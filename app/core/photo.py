"""Extraction de photos depuis les vidéos/photos 360 (voir SPEC.md, section
« Extraction de photos 360 »).

Trois sources :
  - MP4 360° converti : seek précis (``-ss`` avant ``-i``) puis 1 frame ;
  - .OSV brut : stitching calibré d'UNE frame pleine résolution en réutilisant
    ``maps.generate_remap_maps`` + le graphe remap/maskedmerge de stitch.py
    (fallback v360 baseline si pas de calibration) — les cartes sont mises en
    cache disque par (fichier, résolution) ;
  - JPEG 360° de la caméra (équirect 2:1) : utilisé tel quel.

Reprojections (ffmpeg v360, input=e) : ``flat`` (perspective sans étirement,
v_fov calculé depuis h_fov et le ratio), ``cylindrical`` (tour complet 360°),
``equirect360`` (équirect 2:1 + XMP GPano injecté pour une photo sphérique
interactive), ``littleplanet`` (stéréographique regard vers le bas).

Remarque .LRF : la caméra écrit un fichier basse résolution ``.LRF`` à côté de
chaque ``.OSV``. Il pourrait servir de source rapide pour ``nav_proxy`` (si son
contenu est bien un dual-fisheye léger) mais n'a pas pu être testé (non copié
avec l'échantillon) : le proxy est donc toujours généré depuis l'OSV lui-même.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess

# Ratios autorisés pour la projection « flat » (largeur:hauteur).
RATIOS: dict[str, tuple[int, int]] = {
    "16:9": (16, 9), "21:9": (21, 9), "32:9": (32, 9),
    "4:3": (4, 3), "1:1": (1, 1), "9:16": (9, 16),
}

# Filtre v360 baseline (identique à stitch.py mode v360).
_V360_BASE = ("v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:yaw=90:"
              "w={w}:h={h}:interp={interp}")

XMP_MARKER = b"http://ns.adobe.com/xap/1.0/\x00"


class PhotoError(Exception):
    """Erreur explicite d'extraction photo (destinée à l'API : 400/422)."""


# ---------------------------------------------------------------------------
# Aides
# ---------------------------------------------------------------------------

def _even(n: float) -> int:
    v = int(round(n))
    return v if v % 2 == 0 else v + 1


def _run(cmd: list[str], timeout: int = 120, what: str = "ffmpeg") -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise PhotoError(f"{cmd[0]} introuvable sur le système") from exc
    except subprocess.TimeoutExpired as exc:
        raise PhotoError(f"{what} : délai dépassé") from exc
    if proc.returncode != 0:
        raise PhotoError(f"{what} a échoué : {proc.stderr.strip()[-500:]}")


def probe_dims(path: str) -> tuple[int, int]:
    """(largeur, hauteur) du premier flux vidéo/image."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-print_format", "json", path],
            capture_output=True, text=True, timeout=30)
        s = json.loads(proc.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except (subprocess.TimeoutExpired, KeyError, IndexError, ValueError,
            json.JSONDecodeError, FileNotFoundError) as exc:
        raise PhotoError(f"dimensions illisibles : {path}") from exc


def _jpeg_q(quality: int) -> str:
    """quality 1..100 -> échelle mjpeg -q:v (2 = quasi sans perte, 31 = pire)."""
    return str(max(2, min(31, round((100 - int(quality)) * 31 / 100))))


def _source_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".osv":
        return "osv"
    if ext in (".mp4", ".mov", ".m4v"):
        return "mp4"
    if ext in (".jpg", ".jpeg"):
        return "jpg"
    raise PhotoError(f"type de source non géré : {ext or path}")


def default_out_w(source_path: str) -> int:
    """Largeur équirect max de la source (défaut de out_w côté API)."""
    kind = _source_kind(source_path)
    w, h = probe_dims(source_path)
    if kind == "osv":
        return 2 * w  # deux fisheyes 3840 -> équirect 7680
    return w


# ---------------------------------------------------------------------------
# Cache des cartes remap (par fichier + résolution)
# ---------------------------------------------------------------------------

def _maps_cache_dir() -> str:
    from app import config
    d = str(config.cache_dir() / "maps")
    os.makedirs(d, exist_ok=True)
    return d


def _cached_mapset(osv_path: str, out_w: int):
    """MapSet (éventuellement non calibré) depuis le cache disque, sinon généré.

    Clé de cache = (chemin, mtime, résolution). Retourne None si même le
    fallback n'a pas pu être produit (modules absents…)."""
    try:
        from app.core.maps import MapSet, generate_remap_maps
    except ImportError:
        return None

    try:
        mtime = os.path.getmtime(osv_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{osv_path}:{mtime}:{out_w}".encode()).hexdigest()
    cdir = os.path.join(_maps_cache_dir(), key)
    meta_path = os.path.join(cdir, "meta.json")

    if os.path.isfile(meta_path):
        try:
            meta = json.loads(open(meta_path, encoding="utf-8").read())
            if all(os.path.isfile(p) for p in
                   meta["xmaps"] + meta["ymaps"] + [meta["blend_mask"]]):
                return MapSet(**meta)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

    # (Re)génération : calibration extraite de l'OSV, puis cartes.
    from app.core import osv as osv_mod
    os.makedirs(cdir, exist_ok=True)
    try:
        meta_ex = osv_mod.extract_metadata(osv_path, cdir)
        calibration = meta_ex.get("calibration")
    except osv_mod.OsvError:
        calibration = None
    ms = generate_remap_maps(calibration, out_w, out_w // 2, cdir)
    try:
        with open(meta_path, "w", encoding="utf-8") as fp:
            json.dump({"out_w": ms.out_w, "out_h": ms.out_h, "xmaps": ms.xmaps,
                       "ymaps": ms.ymaps, "blend_mask": ms.blend_mask,
                       "calibrated": ms.calibrated}, fp)
    except OSError:
        pass
    return ms


# ---------------------------------------------------------------------------
# 1 frame équirect pleine résolution
# ---------------------------------------------------------------------------

def get_equirect_frame(source_path: str, time_s: float, workdir: str,
                       out_w: int | None = None) -> str:
    """PNG (ou JPEG source) équirectangulaire pleine résolution à ``time_s``."""
    if not os.path.isfile(source_path):
        raise PhotoError(f"fichier introuvable : {source_path}")
    kind = _source_kind(source_path)
    os.makedirs(workdir, exist_ok=True)
    out_png = os.path.join(workdir, "equirect.png")

    if kind == "jpg":
        w, h = probe_dims(source_path)
        if h == 0 or abs(w / h - 2.0) > 0.04:
            raise PhotoError(
                f"la photo {os.path.basename(source_path)} n'est pas équirectangulaire "
                f"2:1 ({w}x{h}) — extraction impossible")
        return source_path  # utilisée telle quelle

    # PNG à compression minimale : l'intermédiaire 8K passe de ~11 s à ~6 s.
    png_fast = ["-compression_level", "1"]

    if kind == "mp4":
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, time_s):.3f}",
               "-i", source_path, "-map", "0:v:0", "-frames:v", "1",
               *png_fast, out_png]
        _run(cmd, timeout=120, what="extraction de frame MP4")
        if not os.path.isfile(out_png):
            raise PhotoError(f"aucune frame à t={time_s:.2f}s (durée dépassée ?)")
        return out_png

    # OSV : stitching d'une frame — calibré si cartes dispo, sinon v360 baseline.
    w_fish, _ = probe_dims(source_path)
    ew = _even(out_w or 2 * w_fish)
    eh = ew // 2
    maps = _cached_mapset(source_path, ew)
    ss = ["-ss", f"{max(0.0, time_s):.3f}"]
    if maps is not None and maps.calibrated:
        cmd = ["ffmpeg", "-y", "-v", "error", *ss, "-i", source_path]
        for i in range(2):
            cmd += ["-i", maps.xmaps[i], "-i", maps.ymaps[i]]
        cmd += ["-i", maps.blend_mask]
        graph = (
            "[0:0]format=gbrp[b0];[b0][1:v][2:v]remap=fill=black[e0];"
            "[0:1]format=gbrp[b1];[b1][3:v][4:v]remap=fill=black[e1];"
            "[5:v]format=gbrp[mk];[e0][e1][mk]maskedmerge[mg];"
            "[mg]format=rgb24[v]"
        )
        cmd += ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1",
                *png_fast, out_png]
        _run(cmd, timeout=180, what="stitching calibré d'une frame OSV")
    else:
        graph = ("[0:0][0:1]hstack[s];[s]"
                 + _V360_BASE.format(w=ew, h=eh, interp="lanczos")
                 + ",format=rgb24[v]")
        cmd = ["ffmpeg", "-y", "-v", "error", *ss, "-i", source_path,
               "-filter_complex", graph, "-map", "[v]", "-frames:v", "1",
               *png_fast, out_png]
        _run(cmd, timeout=180, what="stitching v360 d'une frame OSV")
    if not os.path.isfile(out_png):
        raise PhotoError(f"aucune frame à t={time_s:.2f}s (durée dépassée ?)")
    return out_png


# ---------------------------------------------------------------------------
# Reprojection
# ---------------------------------------------------------------------------

def _parse_ratio(ratio: str) -> tuple[float, float]:
    """Préréglage (16:9…) ou ratio libre « a:b » (a, b > 0, a/b dans [0.2, 8])."""
    if ratio in RATIOS:
        return RATIOS[ratio]
    parts = str(ratio).split(":")
    try:
        if len(parts) != 2:
            raise ValueError
        a, b = float(parts[0]), float(parts[1])
    except ValueError:
        raise PhotoError(
            f"ratio invalide : « {ratio} » — préréglage ({', '.join(RATIOS)}) "
            "ou forme « a:b » avec a et b numériques > 0 attendus") from None
    if not (a > 0 and b > 0):
        raise PhotoError(f"ratio invalide : « {ratio} » — a et b doivent être > 0")
    if not (0.2 <= a / b <= 8.0):
        raise PhotoError(
            f"ratio hors bornes : « {ratio} » (a/b = {a / b:.3g}, autorisé : 0.2 à 8)")
    return a, b


def _norm180(a: float) -> float:
    """Ramène un angle en degrés dans [-180, 180) (v360 refuse yaw hors bornes)."""
    return ((float(a) + 180.0) % 360.0) - 180.0


# ---------------------------------------------------------------------------
# Convention d'angles : visionneuse (champs yaw/pitch/roll + cadre bleu) → v360
# ---------------------------------------------------------------------------
#
# Les champs yaw/pitch/roll (et « yaw de départ » / « rotation ») sont exprimés
# dans la convention de la visionneuse WebGL (viewer.js), c.-à-d. ce que l'aperçu
# plein cadre montre à l'utilisateur. ffmpeg v360 utilise une convention
# DIFFÉRENTE ; sans conversion, la photo extraite ne correspond pas au cadre bleu
# (bug historique : yaw décalé de 180°). Le mapping ci-dessous a été mesuré
# empiriquement (voir work/photofix/) en comparant, sur le MÊME équirect, le rendu
# de la visionneuse et celui de v360 :
#
#   • flat (sphère three.js) : la sphère navigable échantillonne l'équirect à
#     u = yaw/360 (yaw=0 → BORD GAUCHE de l'équirect), alors que v360 output=flat
#     vise le CENTRE (u = 0.5) à yaw=0. D'où un décalage de 180° en lacet, sans
#     miroir. Le tangage est identique (positif = vers le haut de part et d'autre).
#     Le roulis est INVERSÉ (la caméra three.js applique rotateZ(-roll), soit une
#     rotation image opposée à celle de v360).
#         yaw_v360 = yaw + 180 ;  pitch_v360 = pitch ;  roll_v360 = -roll
#     (preuve : work/photofix/sweep.jpg — le lever de soleil cadré à yaw=-147,1
#      côté visionneuse est bien produit par v360 yaw=+32,9 = -147,1 + 180.)
#
#   • cylindrical (shader projpreview) : le shader échantillonne u = 0.5 + yaw/360,
#     MÊME origine que v360 output=cylindrical. Aucun décalage.
#         yaw_v360 = yaw
#
#   • littleplanet (shader stéréographique nadir) : le shader vise le nadir avec un
#     azimut lon = atan2(y,x) + rotation, de CHIRALITÉ INVERSE à v360 output=sg.
#     Pour reproduire exactement l'aperçu il faut un miroir horizontal (hflip) et
#         yaw_v360 = rotation + 90 ,  roll_v360 = 0 , pitch = -90
#     (le hflip est ajouté au filtre par reproject()). La « rotation » arrive dans
#     le paramètre roll_deg (cf. frontend), le champ yaw_deg éventuel est ignoré.
#
#   • equirect360 : aucune orientation.


def _flat_angles_v360(yaw: float, pitch: float, roll: float) -> tuple[float, float, float]:
    """(yaw, pitch, roll) v360 pour la projection flat, depuis la convention visionneuse."""
    return _norm180(yaw + 180.0), _norm180(pitch), _norm180(-roll)


def _flat_dims_and_vfov(out_w: int, ratio: str, h_fov_deg: float) -> tuple[int, int, float]:
    rw, rh = _parse_ratio(ratio)
    w = _even(out_w)
    h = _even(w * rh / rw)
    h_fov = min(140.0, max(30.0, float(h_fov_deg)))
    # perspective correcte (pas d'étirement) : v_fov = 2·atan(tan(h_fov/2)·h/w)
    v_fov = math.degrees(2.0 * math.atan(math.tan(math.radians(h_fov) / 2.0) * h / w))
    return w, h, v_fov


def reproject(equirect_path: str, projection: str, params: dict,
              out_jpg: str, quality: int = 95) -> tuple[int, int]:
    """Reprojette une image équirect complète vers ``out_jpg`` (retourne (w, h))."""
    if not os.path.isfile(equirect_path):
        raise PhotoError(f"équirect introuvable : {equirect_path}")
    src_w, src_h = probe_dims(equirect_path)
    out_w = _even(params.get("out_w") or src_w)
    yaw = float(params.get("yaw_deg") or 0.0)
    pitch = float(params.get("pitch_deg") or 0.0)
    roll = float(params.get("roll_deg") or 0.0)
    q = _jpeg_q(quality)

    os.makedirs(os.path.dirname(os.path.abspath(out_jpg)), exist_ok=True)

    if projection == "flat":
        w, h, v_fov = _flat_dims_and_vfov(out_w, params.get("ratio") or "16:9",
                                          float(params.get("h_fov_deg") or 90.0))
        h_fov = min(140.0, max(30.0, float(params.get("h_fov_deg") or 90.0)))
        # convention visionneuse -> v360 (voir _flat_angles_v360 / commentaire ci-dessus)
        v_yaw, v_pitch, v_roll = _flat_angles_v360(yaw, pitch, roll)
        vf = (f"v360=input=e:output=flat:yaw={v_yaw:g}:pitch={v_pitch:g}:roll={v_roll:g}:"
              f"h_fov={h_fov:g}:v_fov={v_fov:g}:w={w}:h={h}:interp=lanczos")
    elif projection == "cylindrical":
        v_span = min(150.0, max(10.0, float(params.get("v_span_deg") or 60.0)))
        w = _even(out_w)
        h = _even(w * math.tan(math.radians(v_span) / 2.0) / math.pi)
        # cylindrique : le shader d'aperçu partage l'origine de v360 -> yaw inchangé
        vf = (f"v360=input=e:output=cylindrical:yaw={_norm180(yaw):g}:"
              f"h_fov=360:v_fov={v_span:g}:w={w}:h={h}:interp=lanczos")
    elif projection == "equirect360":
        w = _even(out_w)
        h = w // 2
        vf = f"scale={w}:{h}:flags=lanczos" if (w, h) != (src_w, src_h) else "null"
    elif projection == "littleplanet":
        # FOV fixe = celui de l'aperçu WebGL (viewer.js PLANET_FOV_DEG = 250) : le
        # shader de prévisualisation ne l'expose pas, l'extraction doit donc utiliser
        # la même valeur pour que la photo corresponde au cadre affiché.
        fov = 250.0
        w = h = _even(out_w if params.get("out_w") else min(src_w // 2, 4096))
        # regard vers le bas ; la « rotation » de la planète arrive dans roll_deg.
        # Le shader d'aperçu est de chiralité inverse à v360 output=sg : hflip +
        # yaw_v360 = rotation + 90 reproduisent exactement l'aperçu (cf. commentaire).
        v_yaw = _norm180(roll + 90.0)
        vf = (f"v360=input=e:output=sg:pitch=-90:yaw={v_yaw:g}:roll=0:"
              f"h_fov={fov:g}:v_fov={fov:g}:w={w}:h={h}:interp=lanczos,hflip")
    else:
        raise PhotoError(
            f"projection inconnue : {projection} "
            "(choix : flat, cylindrical, equirect360, littleplanet)")

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", equirect_path,
           "-vf", vf + ",format=yuvj420p", "-frames:v", "1", "-q:v", q, out_jpg]
    _run(cmd, timeout=120, what=f"reprojection {projection}")
    if not os.path.isfile(out_jpg):
        raise PhotoError(f"reprojection {projection} : aucune sortie produite")

    if projection == "equirect360":
        _inject_gpano(out_jpg, w, h)
    return w, h


# ---------------------------------------------------------------------------
# XMP GPano (photo sphérique interactive)
# ---------------------------------------------------------------------------

def _gpano_packet(w: int, h: int) -> bytes:
    xmp = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="panoforge">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about=""'
        ' xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"'
        ' GPano:ProjectionType="equirectangular"'
        ' GPano:UsePanoramaViewer="True"'
        f' GPano:CroppedAreaImageWidthPixels="{w}"'
        f' GPano:CroppedAreaImageHeightPixels="{h}"'
        f' GPano:FullPanoWidthPixels="{w}"'
        f' GPano:FullPanoHeightPixels="{h}"'
        ' GPano:CroppedAreaLeftPixels="0"'
        ' GPano:CroppedAreaTopPixels="0"/>'
        '</rdf:RDF></x:xmpmeta>'
        '<?xpacket end="w"?>'
    )
    return xmp.encode("utf-8")


def _inject_gpano(jpg_path: str, w: int, h: int) -> None:
    """Insère le paquet XMP GPano en APP1 après les segments APPn existants."""
    data = open(jpg_path, "rb").read()
    if data[:2] != b"\xff\xd8":
        raise PhotoError(f"{jpg_path} n'est pas un JPEG")
    payload = XMP_MARKER + _gpano_packet(w, h)
    if len(payload) + 2 > 0xFFFF:
        raise PhotoError("paquet XMP trop grand")
    seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload

    # position d'insertion : après SOI et les APP0..APP15 déjà présents
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF and 0xE0 <= data[pos + 1] <= 0xEF:
        (ln,) = struct.unpack(">H", data[pos + 2:pos + 4])
        pos += 2 + ln
    with open(jpg_path, "wb") as fp:
        fp.write(data[:pos] + seg + data[pos:])


# ---------------------------------------------------------------------------
# Proxy de navigation (choisir l'instant dans un OSV)
# ---------------------------------------------------------------------------

def nav_proxy(source_path: str, cache_dir: str) -> str:
    """Proxy équirect ~688 px H.264 ultrafast pour naviguer dans un OSV."""
    if not os.path.isfile(source_path):
        raise PhotoError(f"fichier introuvable : {source_path}")
    if _source_kind(source_path) != "osv":
        raise PhotoError("nav_proxy ne s'applique qu'aux fichiers .OSV")
    os.makedirs(cache_dir, exist_ok=True)
    try:
        mtime = os.path.getmtime(source_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{source_path}:{mtime}:navproxy".encode()).hexdigest()
    out = os.path.join(cache_dir, f"{key}_nav.mp4")
    if os.path.isfile(out):
        return out
    graph = ("[0:0][0:1]hstack[s];[s]"
             + _V360_BASE.format(w=688, h=344, interp="line")
             + ",format=yuv420p[v]")
    tmp = out + ".part"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", source_path,
           "-filter_complex", graph, "-map", "[v]", "-an",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
           "-movflags", "+faststart", "-f", "mp4", tmp]
    _run(cmd, timeout=300, what="génération du proxy de navigation")
    os.replace(tmp, out)
    return out

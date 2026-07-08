"""Constructeur de commandes ffmpeg de stitching pour les .OSV DJI Osmo 360.

Deux modes :

- ``v360`` (baseline validée) : ``hstack`` des deux fisheyes puis filtre ``v360``
  (input=dfisheye, ih_fov=iv_fov=190, yaw=90). Interpolation lanczos/bilinéaire,
  chaîne 10 bits conservée pour HEVC.
- ``calibrated`` : chaque fisheye est reprojeté par le filtre ``remap`` avec les
  cartes issues de la calibration usine (voir maps.py), puis les deux
  équirectangulaires sont fusionnés par ``maskedmerge`` avec un masque en dégradé
  (±5° autour des coutures). Limites connues, mesurées lors de la validation
  (voir work/engine/) :
    * ``remap`` échantillonne au plus proche voisin (pas d'interpolation) : très
      légère perte de piqué vs v360 lanczos, surtout aux basses résolutions ;
    * chaîne 8 bits (gbrp) car remap+maskedmerge y sont les plus robustes ;
    * en contrepartie, la géométrie par objectif (centres optiques, distorsion
      réelle, extrinsèques) et le fondu de ±5° suppriment la marche visible aux
      coutures de la baseline (fusion douce, continuité des objets traversants).
  La parallaxe résiduelle sur les objets très proches (<0,5 m) reste visible dans
  les deux modes (les pupilles des objectifs ne sont pas confondues).

L'audio est copié tel quel. ``-progress pipe:1 -nostats`` est toujours ajouté
pour le suivi par jobs.py.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

try:  # import package (app.core.stitch) ou import à plat (tests/outillage)
    from .maps import MapSet
except ImportError:  # pragma: no cover
    from maps import MapSet  # type: ignore

_HAS_NVENC: bool | None = None


@dataclass
class StitchOptions:
    out_w: int = 7680            # 7680, 6144 ou 3840 (h = w/2)
    codec: str = "hevc"          # "hevc" | "h264"
    encoder: str = "auto"        # "auto"→nvenc si dispo sinon cpu ; "nvenc" | "cpu"
    quality: int = 20            # cq nvenc / crf cpu
    interp: str = "lanczos"      # "lanczos" | "line" (mode v360 uniquement)
    mode: str = "auto"           # "auto" | "v360" | "calibrated"
    fps_out: float | None = None # None = conserver ; ex. 5 pour Street View
    stabilize: bool = False           # stabilisation gyroscopique (phase 2)
    stabilize_mode: str = "horizon"   # "horizon" | "lock" | "smooth"
    stabilize_strength: float = 1.0   # force normalisée dans [0.0, 1.0]


def has_nvenc() -> bool:
    """Vrai si ffmpeg expose hevc_nvenc/h264_nvenc (résultat mis en cache)."""
    global _HAS_NVENC
    if _HAS_NVENC is None:
        _HAS_NVENC = False
        if shutil.which("ffmpeg"):
            try:
                out = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-encoders"],
                    capture_output=True, text=True, timeout=20).stdout
                _HAS_NVENC = "hevc_nvenc" in out or "h264_nvenc" in out
            except (OSError, subprocess.SubprocessError):
                pass
    return _HAS_NVENC


def _resolve_mode(opts: StitchOptions, maps: MapSet | None) -> str:
    if opts.mode in ("v360", "calibrated"):
        return opts.mode
    # auto : calibré si des cartes issues d'une vraie calibration sont dispo.
    if maps is not None and maps.calibrated:
        return "calibrated"
    return "v360"


def _encoder_args(opts: StitchOptions) -> tuple[list[str], str]:
    """Retourne (args codec vidéo, pix_fmt de sortie du filtre)."""
    enc = opts.encoder
    if enc == "auto":
        enc = "nvenc" if has_nvenc() else "cpu"
    q = str(int(opts.quality))
    if opts.codec == "h264":
        pix = "yuv420p"
        if enc == "nvenc":
            args = ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", q, "-b:v", "0",
                    "-preset", "p5"]
        else:
            args = ["-c:v", "libx264", "-crf", q, "-preset", "medium"]
    else:  # hevc
        pix = "yuv420p10le"
        if enc == "nvenc":
            args = ["-c:v", "hevc_nvenc", "-rc", "vbr", "-cq", q, "-b:v", "0",
                    "-preset", "p5", "-tag:v", "hvc1"]
        else:
            args = ["-c:v", "libx265", "-crf", q, "-preset", "medium",
                    "-tag:v", "hvc1"]
    return args, pix


def build_command(input_path: str, output_path: str, opts: StitchOptions,
                  maps: MapSet | None = None,
                  stabilize_cmd: str | None = None) -> list[str]:
    """Construit la commande ffmpeg complète (liste d'arguments).

    ``stabilize_cmd`` : chemin d'un fichier de commandes ``sendcmd`` (généré par
    ``stabilize.build_sendcmd``) pilotant la rotation yaw/pitch/roll par frame.
    Généré par jobs.py quand ``opts.stabilize`` est actif et que l'IMU est
    présente. Injection :
      * mode v360 : le v360 du stitch (unique v360 du graphe) est initialisé en
        rotation NEUTRE (yaw=0:pitch=0:roll=0) — indispensable car les commandes
        v360 se COMPOSENT avec l'orientation d'init — et piloté par ``sendcmd``.
        Les angles fournis intègrent déjà l'alignement baseline yaw=90.
      * mode calibrated : le stitch remap ne contient aucun v360 ; on ajoute un
        v360=e:e dédié (unique), init neutre, piloté par ``sendcmd``.
    Sans ``stabilize_cmd`` le comportement est strictement inchangé (baseline).
    """
    mode = _resolve_mode(opts, maps)
    if mode == "calibrated" and maps is None:
        raise ValueError("mode 'calibrated' demandé sans MapSet")
    out_w = int(opts.out_w)
    out_h = out_w // 2
    if maps is not None and mode == "calibrated" and (
            maps.out_w != out_w or maps.out_h != out_h):
        raise ValueError(
            f"MapSet {maps.out_w}x{maps.out_h} != sortie {out_w}x{out_h}")

    interp = opts.interp if opts.interp in ("lanczos", "line") else "lanczos"
    fps = f"fps={opts.fps_out:g}," if opts.fps_out else ""
    enc_args, pix = _encoder_args(opts)
    stab = bool(stabilize_cmd)
    # échappe la virgule/le point-virgule éventuels du chemin pour filter_complex
    sc = _escape_filter_path(stabilize_cmd) if stab else ""

    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", input_path]

    if mode == "v360":
        if stab:
            # v360 unique du graphe : init NEUTRE + sendcmd (angles pré-fondus
            # avec le yaw=90 baseline). Chaîne 10 bits conservée.
            graph = (
                f"[0:0][0:1]hstack[s];"
                f"[s]{fps}sendcmd=f={sc},"
                f"v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:"
                f"yaw=0:pitch=0:roll=0:w={out_w}:h={out_h}:interp={interp},"
                f"format={pix}[v]"
            )
        else:
            # baseline validée : yaw=90, chaîne 10 bits conservée.
            graph = (
                f"[0:0][0:1]hstack[s];"
                f"[s]{fps}v360=input=dfisheye:output=e:ih_fov=190:iv_fov=190:"
                f"yaw=90:w={out_w}:h={out_h}:interp={interp},format={pix}[v]"
            )
    else:
        for i in range(2):
            cmd += ["-i", maps.xmaps[i], "-i", maps.ymaps[i]]
        cmd += ["-i", maps.blend_mask]
        # entrées : 1=xmap0 2=ymap0 3=xmap1 4=ymap1 5=masque
        graph = (
            f"[0:0]{fps}format=gbrp[b0];"
            f"[b0][1:v][2:v]remap=fill=black[e0];"
            f"[0:1]{fps}format=gbrp[b1];"
            f"[b1][3:v][4:v]remap=fill=black[e1];"
            f"[5:v]format=gbrp[mk];"
            f"[e0][e1][mk]maskedmerge[mg];"
        )
        if stab:
            # v360=e:e dédié (unique v360), init neutre, piloté par sendcmd.
            graph += (
                f"[mg]sendcmd=f={sc},v360=e:e:yaw=0:pitch=0:roll=0:"
                f"w={out_w}:h={out_h}:interp={interp},format={pix}[v]"
            )
        else:
            graph += f"[mg]format={pix}[v]"

    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-c:a", "copy"]
    cmd += enc_args
    cmd += ["-progress", "pipe:1", "-nostats", output_path]
    return cmd


def _escape_filter_path(path: str) -> str:
    """Échappe un chemin pour l'option ``f=`` d'un filtre dans -filter_complex
    (les caractères ``\\ : , ; ' [ ]`` y sont spéciaux)."""
    out = []
    for ch in path:
        if ch in "\\:,;'[]":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)

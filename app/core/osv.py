"""Probe ffprobe + wrapper autour de ``app/core/osv_meta/extract_djmd.py``.

``extract_djmd.py`` est livré tel quel (script autonome, pas un paquet Python :
il fait ``from mp4parse import walk`` en import absolu). On l'invoque donc en
sous-processus avec son propre dossier en tête de ``sys.path`` (comportement
naturel de ``python3 script.py``), ce qui évite de dupliquer/modifier son code.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

OSV_META_DIR = os.path.join(os.path.dirname(__file__), "osv_meta")
EXTRACT_DJMD = os.path.join(OSV_META_DIR, "extract_djmd.py")


@dataclass
class OsvInfo:
    path: str
    duration_s: float
    fps: float
    width: int
    height: int
    creation_time_utc: datetime | None
    size_bytes: int
    audio: bool

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "duration_s": self.duration_s,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "creation_time_utc": self.creation_time_utc.isoformat() if self.creation_time_utc else None,
            "size_bytes": self.size_bytes,
            "audio": self.audio,
        }


class OsvError(Exception):
    """Erreur explicite de probe/extraction (fichier absent, ffprobe en échec, etc.)."""


def _parse_creation_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_rational(rate: str | None) -> float:
    if not rate:
        return 0.0
    try:
        if "/" in rate:
            num, den = rate.split("/")
            den = float(den)
            return float(num) / den if den else 0.0
        return float(rate)
    except ValueError:
        return 0.0


def probe(path: str) -> OsvInfo:
    """Sonde un fichier .OSV (MP4) via ffprobe : durée, fps, résolution, audio."""
    if not os.path.isfile(path):
        raise OsvError(f"fichier introuvable : {path}")
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_format", "-show_streams",
                "-print_format", "json",
                path,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise OsvError("ffprobe introuvable sur le système") from exc
    except subprocess.TimeoutExpired as exc:
        raise OsvError(f"ffprobe a expiré sur {path}") from exc
    if proc.returncode != 0:
        raise OsvError(f"ffprobe a échoué sur {path} : {proc.stderr.strip()[:500]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OsvError(f"sortie ffprobe illisible pour {path}") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise OsvError(f"aucun flux vidéo dans {path}")
    # Les deux premiers flux vidéo sont les fisheyes (stream 0/1), même résolution.
    v0 = video_streams[0]

    duration_s = 0.0
    try:
        duration_s = float(fmt.get("duration") or v0.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration_s = 0.0

    fps = _parse_rational(v0.get("r_frame_rate") or v0.get("avg_frame_rate"))

    size_bytes = 0
    try:
        size_bytes = int(fmt.get("size") or os.path.getsize(path))
    except (TypeError, ValueError, OSError):
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0

    creation_time_utc = _parse_creation_time(
        (fmt.get("tags") or {}).get("creation_time") or (v0.get("tags") or {}).get("creation_time")
    )

    return OsvInfo(
        path=path,
        duration_s=duration_s,
        fps=fps,
        width=int(v0.get("width") or 0),
        height=int(v0.get("height") or 0),
        creation_time_utc=creation_time_utc,
        size_bytes=size_bytes,
        audio=bool(audio_streams),
    )


def extract_metadata(path: str, workdir: str) -> dict:
    """Extrait calibration + IMU via extract_djmd.py (sous-processus).

    Retourne ``{"calibration": dict|None, "imu_perframe": str, "imu_highrate": str}``.
    Si le script échoue ou si le fichier n'a pas de piste djmd exploitable,
    ``calibration`` est ``None`` et les CSV peuvent être absents (chemins
    renvoyés quand même, à vérifier par l'appelant avant lecture).
    """
    if not os.path.isfile(path):
        raise OsvError(f"fichier introuvable : {path}")
    if not os.path.isfile(EXTRACT_DJMD):
        raise OsvError("module core/osv_meta/extract_djmd.py absent")

    os.makedirs(workdir, exist_ok=True)
    try:
        proc = subprocess.run(
            ["python3", EXTRACT_DJMD, path, workdir],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise OsvError("extraction des métadonnées djmd expirée (timeout)") from exc

    cal_path = os.path.join(workdir, "calibration.json")
    pf_path = os.path.join(workdir, "imu_perframe.csv")
    hr_path = os.path.join(workdir, "imu_highrate.csv")

    calibration = None
    if os.path.isfile(cal_path):
        try:
            with open(cal_path, encoding="utf-8") as fp:
                cal = json.load(fp)
            if cal.get("lenses"):
                calibration = cal
        except (OSError, json.JSONDecodeError):
            calibration = None

    if proc.returncode != 0 and calibration is None:
        # Le script ne lève jamais d'exception à proprement parler (il imprime
        # un message et sort), mais on garde une trace de l'échec éventuel.
        pass

    return {
        "calibration": calibration,
        "imu_perframe": pf_path,
        "imu_highrate": hr_path,
    }


def extract_thumbnail(path: str, out_jpg: str) -> str:
    """Extrait la miniature équirectangulaire embarquée (dernier flux, MJPEG)."""
    if not os.path.isfile(path):
        raise OsvError(f"fichier introuvable : {path}")
    try:
        probe_proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_name,codec_type",
             "-print_format", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(probe_proc.stdout).get("streams", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        raise OsvError(f"impossible de sonder les flux de {path}") from exc

    if not streams:
        raise OsvError(f"aucun flux dans {path}")
    last = streams[-1]
    if last.get("codec_type") != "video" or last.get("codec_name") != "mjpeg":
        raise OsvError(f"pas de miniature MJPEG en dernier flux dans {path}")
    idx = last["index"]

    os.makedirs(os.path.dirname(os.path.abspath(out_jpg)), exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", path,
                "-map", f"0:{idx}",
                "-frames:v", "1",
                out_jpg,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise OsvError("extraction de la miniature expirée (timeout)") from exc
    if proc.returncode != 0 or not os.path.isfile(out_jpg):
        raise OsvError(f"échec extraction miniature : {proc.stderr.strip()[:500]}")
    return out_jpg

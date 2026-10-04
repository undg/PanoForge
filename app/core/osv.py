"""ffprobe probe + wrapper around ``app/core/osv_meta/extract_djmd.py``.

``extract_djmd.py`` is shipped as-is (standalone script, not a Python package:
it does ``from mp4parse import walk`` as an absolute import). It is therefore
invoked as a subprocess with its own folder at the head of ``sys.path`` (natural
behavior of ``python3 script.py``), which avoids duplicating/modifying its code.
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
    """Explicit probe/extraction error (missing file, failed ffprobe, etc.)."""


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
    """Probe a .OSV (MP4) file via ffprobe: duration, fps, resolution, audio."""
    if not os.path.isfile(path):
        raise OsvError(f"file not found: {path}")
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
        raise OsvError("ffprobe not found on the system") from exc
    except subprocess.TimeoutExpired as exc:
        raise OsvError(f"ffprobe timed out on {path}") from exc
    if proc.returncode != 0:
        raise OsvError(f"ffprobe failed on {path}: {proc.stderr.strip()[:500]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OsvError(f"unreadable ffprobe output for {path}") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not video_streams:
        raise OsvError(f"no video stream in {path}")
    # The first two video streams are the fisheyes (stream 0/1), same resolution.
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
    """Extract calibration + IMU via extract_djmd.py (subprocess).

    Returns ``{"calibration": dict|None, "imu_perframe": str, "imu_highrate": str}``.
    If the script fails or if the file has no usable djmd track,
    ``calibration`` is ``None`` and the CSVs may be absent (paths
    returned anyway, to be checked by the caller before reading).
    """
    if not os.path.isfile(path):
        raise OsvError(f"file not found: {path}")
    if not os.path.isfile(EXTRACT_DJMD):
        raise OsvError("module core/osv_meta/extract_djmd.py missing")

    os.makedirs(workdir, exist_ok=True)
    try:
        proc = subprocess.run(
            ["python3", EXTRACT_DJMD, path, workdir],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise OsvError("djmd metadata extraction timed out") from exc

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
        # The script never really raises an exception (it prints
        # a message and exits), but we keep a trace of the possible failure.
        pass

    return {
        "calibration": calibration,
        "imu_perframe": pf_path,
        "imu_highrate": hr_path,
    }


def extract_thumbnail(path: str, out_jpg: str) -> str:
    """Extract the embedded equirectangular thumbnail (last stream, MJPEG)."""
    if not os.path.isfile(path):
        raise OsvError(f"file not found: {path}")
    try:
        probe_proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=index,codec_name,codec_type",
             "-print_format", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(probe_proc.stdout).get("streams", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        raise OsvError(f"unable to probe the streams of {path}") from exc

    if not streams:
        raise OsvError(f"no stream in {path}")
    last = streams[-1]
    if last.get("codec_type") != "video" or last.get("codec_name") != "mjpeg":
        raise OsvError(f"no MJPEG thumbnail as last stream in {path}")
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
        raise OsvError("thumbnail extraction timed out") from exc
    if proc.returncode != 0 or not os.path.isfile(out_jpg):
        raise OsvError(f"thumbnail extraction failed: {proc.stderr.strip()[:500]}")
    return out_jpg
